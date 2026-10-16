/** Native ratchet, sealed-sender, group decryption, and durable text ingestion. */
import * as S from "@signalapp/libsignal-client";
import { b64, decode, groupId, serviceId, unb64, unpad } from "./protocol.mjs";
import { persist, stores } from "./store.mjs";

const trustRoots = [
	"BXu6QIKVz5MA8gstzfOgRQGqyLqOwNKHL6INkv3IHWMF",
	"BUkY0I+9+oPgDCn4+Ac6Iu813yvqkDr/ga8DzLxFxuk6",
].map((raw) => S.PublicKey.deserialize(unb64(raw)));

export async function decryptEnvelope(account, envelope, roots = trustRoots) {
	if ([5, 8].includes(envelope.type) || !envelope.content?.length) return null;
	const destination = serviceId(
		envelope.destinationServiceIdBinary,
		envelope.destinationServiceId,
	);
	const keys = stores(account, destination?.startsWith("PNI:") ? "pni" : "aci");
	let sender = serviceId(
		envelope.sourceServiceIdBinary,
		envelope.sourceServiceId,
	);
	let device = envelope.sourceDeviceId;
	let bytes = envelope.content;
	let certificate;
	let messageType = envelope.type === 1 ? 2 : envelope.type;
	if (envelope.type === 6) {
		const sealed = await S.sealedSenderDecryptToUsmc(bytes, keys.identity);
		certificate = sealed.senderCertificate();
		if (
			!certificate.validateWithTrustRoots(
				roots,
				envelope.serverTimestamp || Date.now(),
			)
		)
			throw new Error("Invalid sealed-sender certificate");
		sender = certificate.senderUuid();
		device = certificate.senderDeviceId();
		bytes = sealed.contents();
		messageType = sealed.msgType();
	}
	if (!sender || !device) throw new Error("Missing sender identity");
	const address = S.ProtocolAddress.new(sender, device);
	let plaintext;
	if (messageType === 2)
		plaintext = await S.signalDecrypt(
			S.SignalMessage.deserialize(bytes),
			address,
			keys.local,
			keys.session,
			keys.identity,
		);
	else if (messageType === 3)
		plaintext = await S.signalDecryptPreKey(
			S.PreKeySignalMessage.deserialize(bytes),
			address,
			keys.local,
			keys.session,
			keys.identity,
			keys.prekey,
			keys.signed,
			keys.kyber,
		);
	else if (messageType === 7)
		plaintext = await S.groupDecrypt(address, keys.sender, bytes);
	else throw new Error(`Unsupported encrypted message type ${messageType}`);
	if (certificate) {
		const identity = await keys.identity.getIdentity(address);
		if (!identity?.equals(certificate.key()))
			throw new Error(
				"Sealed-sender certificate identity does not match the message sender",
			);
	}
	const content = decode("Content", unpad(plaintext));
	if (content.senderKeyDistributionMessage?.length) {
		await S.processSenderKeyDistributionMessage(
			address,
			S.SenderKeyDistributionMessage.deserialize(
				content.senderKeyDistributionMessage,
			),
			keys.sender,
		);
	}
	// Only the account owner may change local contacts through a device sync.
	if (content.syncMessage && sender !== account.aci)
		throw new Error("Rejected sync from a different account");
	return {
		sender,
		device,
		content,
		timestamp: envelope.clientTimestamp || envelope.serverTimestamp,
	};
}

export function ingestText(db, account, chat, sender, timestamp, message) {
	const owner = account.number || account.aci;
	if (message.delete?.targetSentTimestamp)
		db.prepare(
			"DELETE FROM messages WHERE account=? AND chat=? AND sender=? AND timestamp=?",
		).run(owner, chat, sender, message.delete.targetSentTimestamp);
	if (!message.body || message.expireTimer || message.isViewOnce) return false;
	db.prepare("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?)").run(
		owner,
		chat,
		sender,
		timestamp,
		message.body,
	);
	return true;
}

export function normalized(account, decoded) {
	const sync = decoded.content.syncMessage?.sent;
	const data = sync?.message || decoded.content.dataMessage;
	if (!data) return null;
	const context = data.groupV2;
	const destination = sync
		? serviceId(sync.destinationServiceIdBinary, sync.destinationServiceId) ||
			account.aci
		: decoded.sender;
	const chat = context?.masterKey?.length
		? groupId(context.masterKey)
		: destination;
	if (
		context?.masterKey?.length &&
		!account.groups.some((group) => group.id === chat)
	) {
		account.groups.push({
			id: chat,
			name: chat,
			masterKey: b64(context.masterKey),
			revision: context.revision || 0,
			members: [],
		});
	}
	const group = account.groups.find((group) => group.id === chat);
	if (group && (context?.revision || 0) > group.revision) {
		group.revision = context.revision;
		group.members = [];
	}
	if (Object.hasOwn(data, "expireTimer")) {
		const contact = account.contacts.find((c) =>
			[c.uuid, c.number].includes(chat),
		);
		// Outgoing messages must preserve the conversation's latest disappearing timer.
		if (group) group.expiration = data.expireTimer;
		else if (contact) contact.expiration = data.expireTimer;
		else
			account.contacts.push({
				uuid: chat,
				name: chat,
				expiration: data.expireTimer,
			});
	}
	return {
		chat,
		sender: decoded.sender,
		timestamp: data.timestamp || sync?.timestamp || decoded.timestamp,
		data,
	};
}

export async function drain(db, account) {
	let decodedCount = 0;
	const failures = [];
	for (const row of db
		.prepare("SELECT * FROM native_queue WHERE account=? ORDER BY rowid")
		.all(account.aci)) {
		const snapshot = structuredClone(account);
		try {
			const decoded = await decryptEnvelope(
				account,
				decode("Envelope", row.envelope),
			);
			db.exec("BEGIN IMMEDIATE");
			if (decoded) {
				const item = normalized(account, decoded);
				if (
					item &&
					ingestText(
						db,
						account,
						item.chat,
						item.sender,
						item.timestamp,
						item.data,
					)
				)
					decodedCount++;
			}
			persist(db, account);
			db.prepare("INSERT OR IGNORE INTO native_seen VALUES (?,?)").run(
				account.aci,
				row.id,
			);
			db.prepare("DELETE FROM native_queue WHERE account=? AND id=?").run(
				account.aci,
				row.id,
			);
			db.exec("COMMIT");
		} catch (error) {
			if (db.isTransaction) db.exec("ROLLBACK");
			Object.assign(account, snapshot);
			failures.push({ id: row.id, error: error.message });
		}
	}
	return { decoded: decodedCount, pending: failures.length, failures };
}
