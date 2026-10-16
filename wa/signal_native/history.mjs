/** Authenticated linked-device history archives and text-only inbox imports. */
import { createDecipheriv, createHmac, timingSafeEqual } from "node:crypto";
import { gunzipSync } from "node:zlib";
import { Aci } from "@signalapp/libsignal-client";
import { BackupKey } from "@signalapp/libsignal-client/dist/AccountKeys.js";
import {
	MessageBackupKey,
	OnlineBackupValidator,
	Purpose,
} from "@signalapp/libsignal-client/dist/messageBackup.js";
import { cdnRequest, request } from "./network.mjs";
import {
	b64,
	decode,
	delimited,
	groupId,
	serviceId,
	unb64,
} from "./protocol.mjs";
import { persist } from "./store.mjs";

const limit = 256 * 1024 * 1024;

export function decryptArchive(encrypted, key, aci) {
	if (encrypted.length < 64 || encrypted.length > limit)
		throw new Error("Invalid history archive length");
	const backupKey = new BackupKey(key);
	const material = new MessageBackupKey({
		backupKey,
		backupId: backupKey.deriveBackupId(Aci.fromUuid(aci)),
	});
	const authenticated = encrypted.subarray(0, -32);
	const actual = createHmac("sha256", material.hmacKey)
		.update(authenticated)
		.digest();
	if (!timingSafeEqual(actual, encrypted.subarray(-32)))
		throw new Error("History archive authentication failed");
	const decipher = createDecipheriv(
		"aes-256-cbc",
		material.aesKey,
		encrypted.subarray(0, 16),
	);
	const plaintext = Buffer.concat([
		decipher.update(encrypted.subarray(16, -32)),
		decipher.final(),
	]);
	return gunzipSync(plaintext, { maxOutputLength: limit });
}

export function validateArchive(plaintext) {
	const frames = delimited(plaintext);
	if (!frames.length) throw new Error("History archive is empty");
	const validator = new OnlineBackupValidator(
		frames[0],
		Purpose.DeviceTransfer,
	);
	for (const frame of frames.slice(1)) validator.addFrame(frame);
	validator.finalize();
	return frames.slice(1).map((frame) => decode("signalbackups.Frame", frame));
}

export function importFrames(db, account, frames) {
	const recipients = new Map();
	const chats = new Map();
	let imported = 0;
	for (const frame of frames) {
		const recipient = frame.recipient;
		if (recipient) {
			if (recipient.self) recipients.set(recipient.id, account.aci);
			else if (recipient.contact) {
				const c = recipient.contact;
				const id = c.aci?.length
					? serviceId(c.aci)
					: c.e164
						? `+${c.e164}`
						: undefined;
				if (!id) continue;
				recipients.set(recipient.id, id);
				const contact = {
					uuid: c.aci?.length ? id : null,
					number: c.e164 ? `+${c.e164}` : null,
					name:
						[
							c.systemGivenName || c.profileGivenName,
							c.systemFamilyName || c.profileFamilyName,
						]
							.filter(Boolean)
							.join(" ") ||
						c.username ||
						id,
				};
				const existing = account.contacts.find(
					(c) => c.uuid === id || c.number === id,
				);
				if (existing) Object.assign(existing, contact);
				else account.contacts.push(contact);
			} else if (recipient.group?.masterKey?.length) {
				const group = recipient.group;
				const id = groupId(group.masterKey);
				recipients.set(recipient.id, id);
				const record = {
					id,
					name: group.snapshot?.title?.title || id,
					masterKey: b64(group.masterKey),
					revision: group.snapshot?.version || 0,
					expiration:
						group.snapshot?.disappearingMessagesTimer
							?.disappearingMessagesDuration || 0,
					members: (group.snapshot?.members || [])
						.map((m) => serviceId(m.userId))
						.filter(Boolean),
				};
				const existing = account.groups.find((g) => g.id === id);
				if (existing) Object.assign(existing, record);
				else account.groups.push(record);
			}
		}
		if (frame.chat) {
			const id = recipients.get(frame.chat.recipientId);
			chats.set(frame.chat.id, id);
			const conversation =
				account.contacts.find((c) => [c.uuid, c.number].includes(id)) ||
				account.groups.find((g) => g.id === id);
			if (conversation && Object.hasOwn(frame.chat, "expirationTimerMs"))
				conversation.expiration = Math.ceil(
					frame.chat.expirationTimerMs / 1000,
				);
		}
		const item = frame.chatItem;
		if (!item || item.expiresInMs || !item.standardMessage?.text?.body)
			continue;
		const chat = chats.get(item.chatId);
		const sender = recipients.get(item.authorId);
		if (!chat || !sender)
			throw new Error("History message references an unknown chat or sender");
		const result = db
			.prepare("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?)")
			.run(
				account.number || account.aci,
				chat,
				sender,
				item.dateSent,
				item.standardMessage.text.body,
			);
		imported += result.changes;
	}
	return imported;
}

export async function importHistory(db, account, chat) {
	if (account.historyImported && !account.historyKey)
		return { imported: 0, already_imported: true };
	if (!account.historyKey)
		throw new Error(
			"History transfer needs a new native pairing: run signal-cli login and choose Transfer Message History on your phone. Existing messages and credentials remain intact.",
		);
	if (!account.historyArchive) {
		const deadline = Date.now() + 300000;
		while (Date.now() < deadline) {
			const response = await request(
				chat,
				"GET",
				"/v1/devices/transfer_archive?timeout=30",
			);
			if (response.status === 204) continue;
			if (response.body?.error)
				throw new Error(
					`Phone declined history transfer: ${response.body.error}`,
				);
			account.historyArchive = response.body;
			persist(db, account);
			break;
		}
	}
	const location = account.historyArchive;
	if (![0, 2, 3].includes(location?.cdn) || typeof location.key !== "string")
		throw new Error("No history archive is available from the phone");
	// The server supplies a CDN-relative key, never an arbitrary destination URL.
	const host =
		location.cdn === 0 ? "cdn.signal.org" : `cdn${location.cdn}.signal.org`;
	const key = encodeURIComponent(location.key);
	const response = await cdnRequest(`https://${host}/attachments/${key}`, {
		signal: AbortSignal.timeout(120000),
	});
	if (!response.ok)
		throw new Error(`Signal history download returned HTTP ${response.status}`);
	const chunks = [];
	let length = 0;
	for await (const chunk of response.body) {
		length += chunk.length;
		if (length > limit)
			throw new Error("History archive exceeds the 256 MiB import limit");
		chunks.push(chunk);
	}
	const frames = validateArchive(
		decryptArchive(
			Buffer.concat(chunks),
			unb64(account.historyKey),
			account.aci,
		),
	);
	const snapshot = structuredClone(account);
	db.exec("BEGIN IMMEDIATE");
	try {
		const imported = importFrames(db, account, frames);
		delete account.historyKey;
		delete account.historyArchive;
		account.historyImported = Date.now();
		persist(db, account);
		db.exec("COMMIT");
		return { imported };
	} catch (error) {
		db.exec("ROLLBACK");
		Object.assign(account, snapshot);
		throw error;
	}
}
