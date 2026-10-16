/** Signal device provisioning with official cryptographic primitives. */
import {
	createCipheriv,
	createDecipheriv,
	createHmac,
	randomBytes,
	randomInt,
	timingSafeEqual,
} from "node:crypto";
import * as S from "@signalapp/libsignal-client";
import { importHistory } from "./history.mjs";
import { connect, network, serviceRequest } from "./network.mjs";
import { b64, decode, encode, serviceId } from "./protocol.mjs";
import { accounts, persist } from "./store.mjs";

const hmac = (key, value) => createHmac("sha256", key).update(value).digest();

export function decryptProvision(privateKey, rawEnvelope) {
	const envelope = decode("ProvisionEnvelope", rawEnvelope);
	const body = envelope.body;
	if (!body || body.length < 65 || body[0] !== 1)
		throw new Error("Invalid provisioning envelope");
	const shared = privateKey.agree(S.PublicKey.deserialize(envelope.publicKey));
	const keys = Buffer.from(
		S.hkdf(
			64,
			shared,
			Buffer.from("TextSecure Provisioning Message"),
			Buffer.alloc(32),
		),
	);
	const mac = hmac(keys.subarray(32), body.subarray(0, -32));
	if (!timingSafeEqual(mac, body.subarray(-32)))
		throw new Error("Provisioning authentication failed");
	const cipher = createDecipheriv(
		"aes-256-cbc",
		keys.subarray(0, 32),
		body.subarray(1, 17),
	);
	const plaintext = Buffer.concat([
		cipher.update(body.subarray(17, -32)),
		cipher.final(),
	]);
	return decode("ProvisionMessage", plaintext);
}

export function encryptedDeviceName(name, identity) {
	const ephemeral = S.PrivateKey.generate();
	const secret = ephemeral.agree(identity.getPublicKey());
	const data = Buffer.from(name, "utf8");
	if (data.length > 100)
		throw new Error("Device name must be at most 100 UTF-8 bytes");
	const iv = hmac(hmac(secret, Buffer.from("auth")), data).subarray(0, 16);
	const key = hmac(hmac(secret, Buffer.from("cipher")), iv);
	const cipher = createCipheriv("aes-256-ctr", key, Buffer.alloc(16));
	return b64(
		encode("DeviceName", {
			ephemeralPublic: ephemeral.getPublicKey().getPublicKeyBytes(),
			syntheticIv: iv,
			ciphertext: Buffer.concat([cipher.update(data), cipher.final()]),
		}),
	);
}

export function createKeys(identityBytes) {
	const identity = S.PrivateKey.deserialize(identityBytes);
	const signedKey = S.PrivateKey.generate();
	const signedSignature = identity.sign(signedKey.getPublicKey().serialize());
	const signed = S.SignedPreKeyRecord.new(
		1,
		Date.now(),
		signedKey.getPublicKey(),
		signedKey,
		signedSignature,
	);
	const kem = S.KEMKeyPair.generate();
	const signature = identity.sign(kem.getPublicKey().serialize());
	const pq = S.KyberPreKeyRecord.new(1, Date.now(), kem, signature);
	return {
		state: {
			identity: b64(identity.serialize()),
			registrationId: randomInt(1, 16384),
			prekeys: {},
			signed: { 1: b64(signed.serialize()) },
			kyber: { 1: { record: b64(pq.serialize()), lastResort: true } },
			sessions: {},
			identities: {},
			senders: {},
			usedKyber: {},
		},
		signed: {
			keyId: 1,
			publicKey: b64(signed.publicKey().serialize()),
			signature: b64(signed.signature()),
		},
		pq: {
			keyId: 1,
			publicKey: b64(pq.publicKey().serialize()),
			signature: b64(pq.signature()),
		},
	};
}

export function deviceAttributes(name, aciKeys, pniKeys) {
	return {
		fetchesMessages: true,
		name,
		registrationId: aciKeys.state.registrationId,
		...(pniKeys ? { pniRegistrationId: pniKeys.state.registrationId } : {}),
		// Accounts with username sync enabled reject clients that downgrade it.
		capabilities: {
			spqr: true,
			profiles_v2: true,
			usernameChangeSyncMessage: true,
		},
	};
}

export async function link(db, name, history, output) {
	const privateKey = S.PrivateKey.generate();
	let resolveEnvelope;
	let rejectEnvelope;
	const received = new Promise((resolve, reject) => {
		resolveEnvelope = resolve;
		rejectEnvelope = reject;
	});
	const timer = setTimeout(
		() =>
			rejectEnvelope(
				new Error("Pairing timed out; run signal-cli login again"),
			),
		180000,
	);
	const net = network();
	let provisioning;
	try {
		provisioning = await net.connectProvisioning(
			{
				onReceivedAddress(address, ack) {
					const uri = new URL("sgnl://linkdevice");
					uri.searchParams.set("uuid", address);
					uri.searchParams.set(
						"pub_key",
						b64(privateKey.getPublicKey().serialize()),
					);
					if (history) uri.searchParams.set("capabilities", "backup5");
					output({ type: "link", uri: uri.toString() });
					ack.send(200);
				},
				onReceivedEnvelope(envelope, ack) {
					ack.send(200);
					resolveEnvelope(Buffer.from(envelope));
				},
				onConnectionInterrupted(error) {
					if (error)
						rejectEnvelope(
							new Error(
								"Pairing connection closed before phone approval; run signal-cli login again",
								{ cause: error },
							),
						);
				},
			},
			{ abortSignal: AbortSignal.timeout(30000) },
		);
		const message = decryptProvision(privateKey, await received);
		const aci = serviceId(message.aciBinary, message.aci);
		if (
			!aci ||
			!message.aciIdentityKeyPrivate?.length ||
			!message.provisioningCode
		)
			throw new Error("Provisioning message is missing required keys");
		const aciKeys = createKeys(message.aciIdentityKeyPrivate);
		const pniKeys = message.pniIdentityKeyPrivate?.length
			? createKeys(message.pniIdentityKeyPrivate)
			: undefined;
		const password = randomBytes(24).toString("base64url");
		const deviceName = encryptedDeviceName(
			name,
			S.PrivateKey.deserialize(message.aciIdentityKeyPrivate),
		);
		const account = {
			aci,
			pni: serviceId(message.pniBinary, message.pni),
			number: message.number,
			password,
			profileKey: b64(message.profileKey || []),
			contacts: [],
			groups: [],
			keys: { aci: aciKeys.state, ...(pniKeys ? { pni: pniKeys.state } : {}) },
			historyKey:
				history && message.ephemeralBackupKey?.length
					? b64(message.ephemeralBackupKey)
					: undefined,
		};
		const result = await serviceRequest(
			"PUT",
			"/v1/devices/link",
			{
				verificationCode: message.provisioningCode,
				accountAttributes: deviceAttributes(deviceName, aciKeys, pniKeys),
				aciSignedPreKey: aciKeys.signed,
				aciPqLastResortPreKey: aciKeys.pq,
				...(pniKeys
					? {
							pniSignedPreKey: pniKeys.signed,
							pniPqLastResortPreKey: pniKeys.pq,
						}
					: {}),
			},
			{
				Authorization: `Basic ${Buffer.from(`${aci}:${password}`).toString("base64")}`,
			},
		);
		if (!result.body?.deviceId)
			throw new Error("Signal linking returned no device ID");
		account.deviceId = result.body.deviceId;
		if (result.body.uuid && result.body.uuid !== aci)
			throw new Error("Linked account identity mismatch");
		const previous = accounts(db).find((a) => a.aci === aci);
		if (previous) {
			db.exec(
				"CREATE TABLE IF NOT EXISTS native_retired_state (account TEXT, device INTEGER, value TEXT, PRIMARY KEY(account,device))",
			);
			db.prepare(
				"INSERT OR REPLACE INTO native_retired_state VALUES (?,?,?)",
			).run(aci, previous.deviceId, JSON.stringify(previous));
			account.contacts = previous.contacts;
			account.groups = previous.groups;
		}
		persist(db, account);
		output({
			type: "linked",
			account: account.number || account.aci,
			deviceId: account.deviceId,
		});
		if (account.historyKey) {
			const { chat } = await connect(db, account);
			try {
				output({
					type: "history",
					...(await importHistory(db, account, chat)),
				});
			} finally {
				await chat.disconnect();
			}
		} else if (history)
			output({
				type: "history-unavailable",
				reason: "The phone did not provide a history-transfer key.",
			});
		return account;
	} finally {
		clearTimeout(timer);
		// The server may close provisioning after delivery or QR expiry.
		if (provisioning) await provisioning.disconnect().catch(() => undefined);
	}
}
