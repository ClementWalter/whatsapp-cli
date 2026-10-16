/** Cryptographic interoperability, archive integrity, and durable inbox regressions. */

import assert from "node:assert/strict";
import { createCipheriv, createHmac, randomBytes } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { gzipSync } from "node:zlib";
import * as S from "@signalapp/libsignal-client";
import { BackupKey } from "@signalapp/libsignal-client/dist/AccountKeys.js";
import { MessageBackupKey } from "@signalapp/libsignal-client/dist/messageBackup.js";
import {
	decryptArchive,
	importFrames,
	importHistory,
	validateArchive,
} from "../history.mjs";
import { createKeys, decryptProvision, deviceAttributes } from "../link.mjs";
import { cdnRequest } from "../network.mjs";
import { b64, encode, pad, type, unpad } from "../protocol.mjs";
import { decryptEnvelope, drain, ingestText, normalized } from "../receive.mjs";
import { argumentsFor, execute } from "../runtime.mjs";
import { encryptForDevices, send } from "../send.mjs";
import { database, persist, select, stores } from "../store.mjs";

const aliceId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const bobId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

test("device registration preserves required account capabilities", () => {
	const keys = { state: { registrationId: 123 } };
	assert.deepEqual(deviceAttributes("encrypted-name", keys).capabilities, {
		spqr: true,
		profiles_v2: true,
		usernameChangeSyncMessage: true,
	});
});

test("username sync invalidates the account username hint", () => {
	const a = account();
	a.username = "old.123";
	normalized(a, { content: { syncMessage: { usernameChange: {} } } });
	assert.equal(Object.hasOwn(a, "username"), false);
});

test("completed history imports are safe to retry", async () => {
	const a = account();
	a.historyImported = Date.now();
	assert.deepEqual(await importHistory(null, a, null), {
		imported: 0,
		already_imported: true,
	});
});

test("CDN transport rejects plaintext HTTP", () => {
	assert.throws(() => cdnRequest("http://example.invalid/"), /require HTTPS/);
});

test("incoming messages update the outgoing disappearing timer", () => {
	const a = account();
	a.contacts.push({ uuid: bobId, expiration: 0 });
	normalized(a, {
		sender: bobId,
		content: {
			dataMessage: { timestamp: 123, body: "expires", expireTimer: 60 },
		},
	});
	assert.equal(a.contacts[0].expiration, 60);
});

test("history restores conversation disappearing timers", (t) => {
	const { db } = inbox(t);
	const a = account();
	const frames = historyFrames();
	frames[2].chat.expirationTimerMs = 60000;
	importFrames(db, a, frames);
	assert.equal(a.contacts[0].expiration, 60);
});

function account(aci = aliceId) {
	return {
		aci,
		number: aci === aliceId ? "+12025550101" : "+12025550102",
		password: "test-password",
		deviceId: 2,
		contacts: [],
		groups: [],
		keys: { aci: createKeys(S.PrivateKey.generate().serialize()).state },
	};
}

function inbox(t) {
	const directory = fs.mkdtempSync(
		path.join(os.tmpdir(), "signal-native-test-"),
	);
	const db = database(directory);
	t.after(() => {
		db.close();
		fs.rmSync(directory, { recursive: true, force: true });
	});
	return { db, directory };
}

async function encryptedMessage() {
	const alice = account();
	const bob = account(bobId);
	const a = stores(alice);
	const b = stores(bob);
	const signed = await b.signed.getSignedPreKey(1);
	const pq = await b.kyber.getKyberPreKey(1);
	const bundle = S.PreKeyBundle.new(
		bob.keys.aci.registrationId,
		2,
		null,
		null,
		1,
		signed.publicKey(),
		signed.signature(),
		(await b.identity.getIdentityKey()).getPublicKey(),
		1,
		pq.publicKey(),
		pq.signature(),
	);
	await S.processPreKeyBundle(bundle, b.local, a.local, a.session, a.identity);
	const content = encode("Content", {
		dataMessage: { body: "hello", timestamp: 123 },
	});
	const cipher = await S.signalEncrypt(
		pad(content),
		b.local,
		a.local,
		a.session,
		a.identity,
	);
	return {
		alice,
		bob,
		a,
		b,
		envelope: {
			type: 3,
			sourceServiceId: aliceId,
			sourceDeviceId: 2,
			destinationServiceId: bobId,
			clientTimestamp: 123,
			content: cipher.serialize(),
		},
	};
}

test("message padding round-trips binary payloads", () => {
	assert.deepEqual(
		unpad(pad(Buffer.from([0, 128, 255, 0]))),
		Buffer.from([0, 128, 255, 0]),
	);
});

test("invalid padding is rejected", () => {
	assert.throws(() => unpad(Buffer.from([1, 2, 0])), /padding/);
});

test("PQXDH message decrypts through the native receive path", async () => {
	const { bob, envelope } = await encryptedMessage();
	assert.equal(
		(await decryptEnvelope(bob, envelope)).content.dataMessage.body,
		"hello",
	);
});

test("ratchets survive serialization and reload", async () => {
	const { bob, envelope } = await encryptedMessage();
	const restored = JSON.parse(JSON.stringify(bob));
	assert.equal(
		(await decryptEnvelope(restored, envelope)).content.dataMessage.body,
		"hello",
	);
});

test("ciphertext tampering fails authentication", async () => {
	const { bob, envelope } = await encryptedMessage();
	const changed = Buffer.from(envelope.content);
	changed[changed.length - 10] ^= 1;
	await assert.rejects(() =>
		decryptEnvelope(bob, { ...envelope, content: changed }),
	);
});

test("encrypted messages commit inbox and ratchet together", async (t) => {
	const { db } = inbox(t);
	const { bob, envelope } = await encryptedMessage();
	persist(db, bob);
	db.prepare("INSERT INTO native_queue VALUES (?,?,?,?)").run(
		bob.aci,
		"message-id",
		encode("Envelope", envelope),
		123,
	);
	await drain(db, bob);
	assert.equal(db.prepare("SELECT body FROM messages").get().body, "hello");
});

test("failed decrypt retains the encrypted queue item", async (t) => {
	const { db } = inbox(t);
	const bob = account(bobId);
	persist(db, bob);
	db.prepare("INSERT INTO native_queue VALUES (?,?,?,?)").run(
		bob.aci,
		"message-id",
		encode("Envelope", {
			type: 3,
			sourceServiceId: aliceId,
			sourceDeviceId: 2,
			content: Buffer.from("invalid"),
		}),
		123,
	);
	await drain(db, bob);
	assert.equal(
		db.prepare("SELECT count(*) AS count FROM native_queue").get().count,
		1,
	);
});

test("failed decrypt retains the committed ratchet state", async (t) => {
	const { db } = inbox(t);
	const bob = account(bobId);
	persist(db, bob);
	const previous = JSON.stringify(bob);
	db.prepare("INSERT INTO native_queue VALUES (?,?,?,?)").run(
		bob.aci,
		"message-id",
		encode("Envelope", {
			type: 3,
			sourceServiceId: aliceId,
			sourceDeviceId: 2,
			content: Buffer.from("invalid"),
		}),
		123,
	);
	await drain(db, bob);
	assert.equal(JSON.stringify(select(db, bob.aci)), previous);
});

test("sealed sender group ciphertext decrypts with a validated sender certificate", async () => {
	const { bob, a, b } = await encryptedMessage();
	const root = S.PrivateKey.generate();
	const server = S.PrivateKey.generate();
	const serverCertificate = S.ServerCertificate.new(
		1,
		server.getPublicKey(),
		root,
	);
	const senderCertificate = S.SenderCertificate.new(
		aliceId,
		null,
		2,
		(await a.identity.getIdentityKey()).getPublicKey(),
		Date.now() + 60000,
		serverCertificate,
		server,
	);
	const distribution = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
	const skdm = await S.SenderKeyDistributionMessage.create(
		a.local,
		distribution,
		a.sender,
	);
	await S.processSenderKeyDistributionMessage(a.local, skdm, b.sender);
	await b.identity.saveIdentity(
		a.local,
		(await a.identity.getIdentityKey()).getPublicKey(),
	);
	const encrypted = await S.groupEncrypt(
		a.local,
		distribution,
		a.sender,
		pad(
			encode("Content", {
				dataMessage: { body: "group text", timestamp: 124 },
			}),
		),
	);
	const sealed = await S.sealedSenderEncrypt(
		S.UnidentifiedSenderMessageContent.new(
			encrypted,
			senderCertificate,
			0,
			null,
		),
		b.local,
		a.identity,
	);
	const result = await decryptEnvelope(
		bob,
		{ type: 6, content: sealed, serverTimestamp: Date.now() },
		[root.getPublicKey()],
	);
	assert.equal(result.content.dataMessage.body, "group text");
});

test("changed peer identities are rejected", async () => {
	const a = account();
	const keys = stores(a);
	const address = S.ProtocolAddress.new(bobId, 2);
	await keys.identity.saveIdentity(
		address,
		S.PrivateKey.generate().getPublicKey(),
	);
	await assert.rejects(
		() =>
			keys.identity.saveIdentity(
				address,
				S.PrivateKey.generate().getPublicKey(),
			),
		/Identity changed/,
	);
});

test("ephemeral content never enters the inbox", (t) => {
	const { db } = inbox(t);
	ingestText(db, account(), bobId, bobId, 123, {
		body: "secret",
		expireTimer: 60,
	});
	assert.equal(
		db.prepare("SELECT count(*) AS count FROM messages").get().count,
		0,
	);
});

test("view once content never enters the inbox", (t) => {
	const { db } = inbox(t);
	ingestText(db, account(), bobId, bobId, 123, {
		body: "secret",
		isViewOnce: true,
	});
	assert.equal(
		db.prepare("SELECT count(*) AS count FROM messages").get().count,
		0,
	);
});

test("remote deletion is scoped to the original author", (t) => {
	const { db } = inbox(t);
	const a = account();
	ingestText(db, a, "group", aliceId, 123, { body: "keep" });
	ingestText(db, a, "group", bobId, 124, {
		delete: { targetSentTimestamp: 123 },
	});
	assert.equal(db.prepare("SELECT body FROM messages").get().body, "keep");
});

test("sync transcript uses the outgoing destination", () => {
	const a = account();
	const result = normalized(a, {
		sender: aliceId,
		content: {
			syncMessage: {
				sent: {
					destinationServiceId: bobId,
					timestamp: 123,
					message: { body: "outgoing" },
				},
			},
		},
	});
	assert.equal(result.chat, bobId);
});

function historyFrames() {
	return [
		{ recipient: { id: 1, self: {} } },
		{
			recipient: {
				id: 2,
				contact: {
					aci: Buffer.from(bobId.replaceAll("-", ""), "hex"),
					profileGivenName: "Bob",
				},
			},
		},
		{ chat: { id: 1, recipientId: 2 } },
		{
			chatItem: {
				chatId: 1,
				authorId: 2,
				dateSent: 123,
				standardMessage: { text: { body: "past message" } },
			},
		},
	];
}

test("history import maps recipients and chat references", (t) => {
	const { db } = inbox(t);
	importFrames(db, account(), historyFrames());
	assert.equal(
		db.prepare("SELECT body FROM messages").get().body,
		"past message",
	);
});

test("history import is idempotent", (t) => {
	const { db } = inbox(t);
	const a = account();
	importFrames(db, a, historyFrames());
	importFrames(db, a, historyFrames());
	assert.equal(
		db.prepare("SELECT count(*) AS count FROM messages").get().count,
		1,
	);
});

test("history import excludes disappearing text", (t) => {
	const { db } = inbox(t);
	const frames = historyFrames();
	frames[3].chatItem.expiresInMs = 60000;
	importFrames(db, account(), frames);
	assert.equal(
		db.prepare("SELECT count(*) AS count FROM messages").get().count,
		0,
	);
});

function archive() {
	const key = randomBytes(32);
	const bk = new BackupKey(key);
	const material = new MessageBackupKey({
		backupKey: bk,
		backupId: bk.deriveBackupId(S.Aci.fromUuid(aliceId)),
	});
	const iv = randomBytes(16);
	const cipher = createCipheriv("aes-256-cbc", material.aesKey, iv);
	const plain = gzipSync(Buffer.from("authenticated history"));
	const encrypted = Buffer.concat([iv, cipher.update(plain), cipher.final()]);
	return {
		key,
		encrypted: Buffer.concat([
			encrypted,
			createHmac("sha256", material.hmacKey).update(encrypted).digest(),
		]),
	};
}

test("history archive authenticates and decompresses", () => {
	const { key, encrypted } = archive();
	assert.equal(
		decryptArchive(encrypted, key, aliceId).toString(),
		"authenticated history",
	);
});

test("history archive rejects tampering before decryption", () => {
	const { key, encrypted } = archive();
	encrypted[20] ^= 1;
	assert.throws(
		() => decryptArchive(encrypted, key, aliceId),
		/authentication/,
	);
});

test("provisioning rejects unauthenticated key material", () => {
	const local = S.PrivateKey.generate();
	const peer = S.PrivateKey.generate();
	assert.throws(
		() =>
			decryptProvision(
				local,
				encode("ProvisionEnvelope", {
					publicKey: peer.getPublicKey().serialize(),
					body: Buffer.concat([Buffer.from([1]), Buffer.alloc(80)]),
				}),
			),
		/authentication/,
	);
});

test("runtime preserves option-looking message text", () => {
	assert.equal(
		argumentsFor(["send", "--message=--hello", "--", bobId]).options.message,
		"--hello",
	);
});

test("runtime cannot invoke an unknown external command", async (t) => {
	const { db, directory } = inbox(t);
	persist(db, account());
	await assert.rejects(
		() => execute(["--data-dir", directory, "external-backend"]),
		/Unsupported native/,
	);
});

test("runtime reads native accounts without an external binary", async (t) => {
	const { db, directory } = inbox(t);
	persist(db, account());
	let output;
	await execute(["--data-dir", directory, "listAccounts"], (value) => {
		output = value;
	});
	assert.equal(output[0].aci, aliceId);
});

test("provisioning decrypts authenticated identity keys", () => {
	const local = S.PrivateKey.generate();
	const sender = S.PrivateKey.generate();
	const keys = Buffer.from(
		S.hkdf(
			64,
			sender.agree(local.getPublicKey()),
			Buffer.from("TextSecure Provisioning Message"),
			Buffer.alloc(32),
		),
	);
	const iv = randomBytes(16);
	const cipher = createCipheriv("aes-256-cbc", keys.subarray(0, 32), iv);
	const plaintext = encode("ProvisionMessage", {
		aci: aliceId,
		provisioningCode: "test",
		aciIdentityKeyPrivate: S.PrivateKey.generate().serialize(),
	});
	const authenticated = Buffer.concat([
		Buffer.from([1]),
		iv,
		cipher.update(plaintext),
		cipher.final(),
	]);
	const body = Buffer.concat([
		authenticated,
		createHmac("sha256", keys.subarray(32)).update(authenticated).digest(),
	]);
	const result = decryptProvision(
		local,
		encode("ProvisionEnvelope", {
			publicKey: sender.getPublicKey().serialize(),
			body,
		}),
	);
	assert.equal(result.aci, aliceId);
});

test("official backup fixture passes native frame validation", () => {
	const fixture = JSON.parse(
		fs.readFileSync(
			new URL("./fixtures/account-data.json", import.meta.url),
			"utf8",
		),
	);
	const frames = fixture.map((value, index) => {
		const schema = type(
			index === 0 ? "signalbackups.BackupInfo" : "signalbackups.Frame",
		);
		return Buffer.from(
			schema.encodeDelimited(schema.fromObject(value)).finish(),
		);
	});
	assert.equal(
		validateArchive(Buffer.concat(frames)).length,
		fixture.length - 1,
	);
});

async function serviceStub(bob) {
	const keys = stores(bob);
	const signed = await keys.signed.getSignedPreKey(1);
	const pq = await keys.kyber.getKyberPreKey(1);
	return {
		fetch: async () => ({
			status: 200,
			body: Buffer.from(
				JSON.stringify({
					identityKey: b64(
						(await keys.identity.getIdentityKey()).getPublicKey().serialize(),
					),
					devices: [
						{
							deviceId: 2,
							registrationId: bob.keys.aci.registrationId,
							signedPreKey: {
								keyId: 1,
								publicKey: b64(signed.publicKey().serialize()),
								signature: b64(signed.signature()),
							},
							pqPreKey: {
								keyId: 1,
								publicKey: b64(pq.publicKey().serialize()),
								signature: b64(pq.signature()),
							},
						},
					],
				}),
			),
		}),
		async sendMessage(request) {
			this.sent = request;
		},
		async sendSyncMessage() {},
	};
}

test("native outbound prekey bundles produce interoperable ciphertext", async () => {
	const alice = account();
	const bob = account(bobId);
	const chat = await serviceStub(bob);
	const outgoing = await encryptForDevices(
		chat,
		alice,
		bobId,
		encode("Content", { dataMessage: { body: "outbound", timestamp: 123 } }),
	);
	const result = await decryptEnvelope(bob, {
		type: 3,
		sourceServiceId: aliceId,
		sourceDeviceId: 2,
		content: outgoing[0].contents.serialize(),
	});
	assert.equal(result.content.dataMessage.body, "outbound");
});

test("native send preserves disappearing-message timers", async (t) => {
	const { db } = inbox(t);
	const alice = account();
	const bob = account(bobId);
	alice.contacts.push({ uuid: bobId, expiration: 60 });
	const chat = await serviceStub(bob);
	await send(db, alice, chat, bobId, "expires");
	const result = await decryptEnvelope(bob, {
		type: 3,
		sourceServiceId: aliceId,
		sourceDeviceId: 2,
		content: chat.sent.contents[0].contents.serialize(),
	});
	assert.equal(result.content.dataMessage.expireTimer, 60);
});

test("native send does not cache disappearing-message bodies", async (t) => {
	const { db } = inbox(t);
	const alice = account();
	const bob = account(bobId);
	alice.contacts.push({ uuid: bobId, expiration: 60 });
	await send(db, alice, await serviceStub(bob), bobId, "expires");
	assert.equal(
		db.prepare("SELECT count(*) AS count FROM messages").get().count,
		0,
	);
});
