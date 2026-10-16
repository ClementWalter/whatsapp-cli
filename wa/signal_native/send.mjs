/** Direct text, group, attachment, and linked-device synchronization sends. */

import {
	createCipheriv,
	createHash,
	createHmac,
	randomBytes,
} from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import * as S from "@signalapp/libsignal-client";
import { cdnRequest, network, request } from "./network.mjs";
import { encode, pad, unb64 } from "./protocol.mjs";
import { persist, stores } from "./store.mjs";

export async function recipientId(chat, account, query) {
	const known = account.contacts.find((c) =>
		[c.number, c.uuid].includes(query),
	);
	if (known?.uuid) return known.uuid;
	if (query.startsWith("+")) {
		const { body: auth } = await request(chat, "GET", "/v2/directory/auth");
		const result = await network().cdsiLookup(auth, {
			e164s: [query],
			acisAndAccessKeys: [],
		});
		const entry =
			result.entries.get(query) || result.entries.get(query.slice(1));
		if (!entry?.aci)
			throw new Error(
				"Recipient is not discoverable by phone number; use a Signal username or UUID",
			);
		account.contacts.push({ number: query, uuid: entry.aci, name: query });
		return entry.aci;
	}
	if (query.startsWith("u:")) {
		const unauth = await network().connectUnauthenticatedChat({
			onConnectionInterrupted() {},
		});
		try {
			const aci = await unauth.lookUpUsernameHash({
				hash: S.usernames.hash(query.slice(2)),
			});
			if (!aci) throw new Error("Signal username not found");
			return aci.getServiceIdString();
		} finally {
			await unauth.disconnect();
		}
	}
	S.ServiceId.parseFromServiceIdString(query);
	return query;
}

export async function encryptForDevices(chat, account, id, content) {
	const { body: bundle } = await request(
		chat,
		"GET",
		`/v2/keys/${encodeURIComponent(id)}/*`,
	);
	const keys = stores(account);
	const contents = [];
	for (const device of bundle.devices) {
		if (id === account.aci && device.deviceId === account.deviceId) continue;
		const address = S.ProtocolAddress.new(id, device.deviceId);
		let record = await keys.session.getSession(address);
		if (
			!record?.hasCurrentState() ||
			record.remoteRegistrationId() !== device.registrationId
		) {
			if (!device.signedPreKey || !device.pqPreKey)
				throw new Error("Recipient is missing required post-quantum prekeys");
			const prekey = device.preKey;
			const signed = device.signedPreKey;
			const pq = device.pqPreKey;
			const native = S.PreKeyBundle.new(
				device.registrationId,
				device.deviceId,
				prekey?.keyId ?? null,
				prekey ? S.PublicKey.deserialize(unb64(prekey.publicKey)) : null,
				signed.keyId,
				S.PublicKey.deserialize(unb64(signed.publicKey)),
				unb64(signed.signature),
				S.PublicKey.deserialize(unb64(bundle.identityKey)),
				pq.keyId,
				S.KEMPublicKey.deserialize(unb64(pq.publicKey)),
				unb64(pq.signature),
			);
			await S.processPreKeyBundle(
				native,
				address,
				keys.local,
				keys.session,
				keys.identity,
			);
			record = await keys.session.getSession(address);
		}
		contents.push({
			deviceId: device.deviceId,
			registrationId: record.remoteRegistrationId(),
			contents: await S.signalEncrypt(
				pad(content),
				address,
				keys.local,
				keys.session,
				keys.identity,
			),
		});
	}
	return contents;
}

export function encryptAttachment(plaintext) {
	const key = randomBytes(64);
	const iv = randomBytes(16);
	const cipher = createCipheriv("aes-256-cbc", key.subarray(0, 32), iv);
	const padded = Buffer.alloc(
		Math.max(
			541,
			Math.floor(
				1.05 **
					Math.ceil(Math.log(Math.max(1, plaintext.length)) / Math.log(1.05)),
			),
		),
	);
	plaintext.copy(padded);
	const body = Buffer.concat([iv, cipher.update(padded), cipher.final()]);
	const encrypted = Buffer.concat([
		body,
		createHmac("sha256", key.subarray(32)).update(body).digest(),
	]);
	return {
		key,
		encrypted,
		digest: createHash("sha256").update(encrypted).digest(),
	};
}

async function attachment(chat, filename) {
	const stat = fs.statSync(filename);
	if (!stat.isFile() || stat.size > 100 * 1024 * 1024)
		throw new Error(
			"Signal attachments must be regular files of at most 100 MiB",
		);
	const plaintext = fs.readFileSync(filename);
	const encrypted = encryptAttachment(plaintext);
	const form = await chat.getUploadForm({
		uploadSize: BigInt(encrypted.encrypted.length),
	});
	const initialize = await cdnRequest(form.signedUploadUrl, {
		method: "POST",
		headers: Object.fromEntries(form.headers),
		signal: AbortSignal.timeout(60000),
	});
	if (!initialize.ok)
		throw new Error(
			`Signal attachment upload initialization returned HTTP ${initialize.status}`,
		);
	const location = initialize.headers.get("location");
	initialize.body.resume();
	if (!location || new URL(location).protocol !== "https:")
		throw new Error("Invalid Signal attachment upload location");
	const result = await cdnRequest(location, {
		method: "PUT",
		body: encrypted.encrypted,
		signal: AbortSignal.timeout(120000),
	});
	result.body.resume();
	if (!result.ok)
		throw new Error(`Signal attachment upload returned HTTP ${result.status}`);
	return {
		cdnNumber: form.cdn,
		cdnKey: form.key,
		key: encrypted.key,
		digest: encrypted.digest,
		size: plaintext.length,
		contentType: "application/octet-stream",
		fileName: path.basename(filename),
	};
}

export async function sendContent(
	db,
	account,
	chat,
	id,
	content,
	timestamp,
	sync = false,
) {
	const contents = await encryptForDevices(chat, account, id, content);
	// Ratchets advance before a network send; uncertain delivery must never reuse message keys.
	persist(db, account);
	if (!contents.length) return;
	if (sync) await chat.sendSyncMessage({ timestamp, contents, urgent: true });
	else
		await chat.sendMessage({
			destination: S.ServiceId.parseFromServiceIdString(id),
			timestamp,
			contents,
			onlineOnly: false,
			urgent: true,
		});
}

export async function send(db, account, chat, target, body, filename) {
	const timestamp = Date.now();
	const group = account.groups.find((g) => g.id === target);
	if (group && !group.members.length)
		throw new Error(
			"Group membership is unavailable; import the phone history before sending",
		);
	const recipient = group ? null : await recipientId(chat, account, target);
	const expiration =
		group?.expiration ||
		account.contacts.find((c) => c.uuid === recipient)?.expiration ||
		0;
	const message = {
		timestamp,
		body: body || undefined,
		expireTimer: expiration,
		profileKey: account.profileKey ? unb64(account.profileKey) : undefined,
		...(group
			? {
					groupV2: {
						masterKey: unb64(group.masterKey),
						revision: group.revision,
					},
				}
			: {}),
	};
	if (filename) message.attachments = [await attachment(chat, filename)];
	const content = encode("Content", { dataMessage: message });
	const recipients = group
		? group.members.filter((id) => id !== account.aci)
		: [recipient];
	const delivered = [];
	for (const id of recipients) {
		try {
			await sendContent(db, account, chat, id, content, timestamp);
			delivered.push(id);
		} catch (error) {
			throw new Error(
				`Signal send stopped after ${delivered.length}/${recipients.length} recipients; delivery may be uncertain. Message timestamp: ${timestamp}. ${error.message}`,
			);
		}
	}
	let syncError;
	try {
		const sent = {
			timestamp,
			message,
			...(recipient ? { destinationServiceId: recipient } : {}),
		};
		await sendContent(
			db,
			account,
			chat,
			account.aci,
			encode("Content", { syncMessage: { sent } }),
			timestamp,
			true,
		);
	} catch (error) {
		syncError = error.message;
	}
	if (body && !expiration)
		db.prepare("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?)").run(
			account.number || account.aci,
			group?.id || recipient,
			account.aci,
			timestamp,
			body,
		);
	return {
		timestamp,
		recipients: delivered.length,
		...(syncError ? { sync_warning: syncError } : {}),
	};
}
