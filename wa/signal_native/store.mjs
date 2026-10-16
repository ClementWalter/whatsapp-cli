/** Durable Signal credentials, ratchets, and inbox transactions. */
import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import * as S from "@signalapp/libsignal-client";
import { b64, field, serviceId, unb64 } from "./protocol.mjs";

export function database(directory) {
	fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
	const filename = path.join(directory, "inbox.sqlite3");
	fs.closeSync(fs.openSync(filename, "a", 0o600));
	const db = new DatabaseSync(filename);
	db.exec(
		"PRAGMA busy_timeout=5000; CREATE TABLE IF NOT EXISTS native_state (account TEXT PRIMARY KEY, value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS native_queue (account TEXT NOT NULL, id TEXT NOT NULL, envelope BLOB NOT NULL, timestamp INTEGER NOT NULL, PRIMARY KEY(account,id)); CREATE TABLE IF NOT EXISTS native_seen (account TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(account,id)); CREATE TABLE IF NOT EXISTS messages (account TEXT, chat TEXT, sender TEXT, timestamp INTEGER, body TEXT, PRIMARY KEY(account,chat,sender,timestamp));",
	);
	return db;
}

export function persist(db, account) {
	db.prepare("INSERT OR REPLACE INTO native_state VALUES (?,?)").run(
		account.aci,
		JSON.stringify(account),
	);
}

export function accounts(db) {
	return db
		.prepare("SELECT value FROM native_state ORDER BY account")
		.all()
		.map((row) => JSON.parse(row.value));
}

export function select(db, query) {
	const matches = accounts(db).filter(
		(a) => !query || [a.aci, a.number].includes(query),
	);
	if (matches.length !== 1)
		throw new Error(
			"Run signal-cli login, or select one linked account with --account.",
		);
	return matches[0];
}

export function importLegacy(db, directory) {
	const indexPath = path.join(directory, "data", "accounts.json");
	if (accounts(db).length || !fs.existsSync(indexPath)) return;
	const index = JSON.parse(fs.readFileSync(indexPath, "utf8"));
	for (const entry of index.accounts) {
		if (!/^\d+$/.test(entry.path))
			throw new Error("Invalid legacy account filename");
		const configPath = path.join(directory, "data", entry.path);
		const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
		if (!config.registered) continue;
		const old = new DatabaseSync(`${configPath}.d/account.db`, {
			readOnly: true,
		});
		try {
			const account = {
				aci: config.aciAccountData.serviceId,
				pni: config.pniAccountData?.serviceId,
				number: config.number,
				password: config.password,
				deviceId: config.deviceId,
				profileKey: config.profileKey,
				contacts: [],
				groups: [],
				keys: {},
			};
			for (const [kind, id] of [
				["aci", 0],
				["pni", 1],
			]) {
				const source = config[`${kind}AccountData`];
				if (!source) continue;
				const keys = {
					identity: source.identityPrivateKey,
					registrationId: source.registrationId,
					prekeys: {},
					signed: {},
					kyber: {},
					sessions: {},
					identities: {},
					senders: {},
					usedKyber: {},
				};
				account.keys[kind] = keys;
				for (const row of old
					.prepare("SELECT * FROM pre_key WHERE account_id_type=?")
					.all(id)) {
					keys.prekeys[row.key_id] = b64(
						S.PreKeyRecord.new(
							row.key_id,
							S.PublicKey.deserialize(row.public_key),
							S.PrivateKey.deserialize(row.private_key),
						).serialize(),
					);
				}
				for (const row of old
					.prepare("SELECT * FROM signed_pre_key WHERE account_id_type=?")
					.all(id)) {
					keys.signed[row.key_id] = b64(
						S.SignedPreKeyRecord.new(
							row.key_id,
							row.timestamp || 0,
							S.PublicKey.deserialize(row.public_key),
							S.PrivateKey.deserialize(row.private_key),
							row.signature,
						).serialize(),
					);
				}
				for (const row of old
					.prepare("SELECT * FROM kyber_pre_key WHERE account_id_type=?")
					.all(id))
					keys.kyber[row.key_id] = {
						record: b64(row.serialized),
						lastResort: !!row.is_last_resort,
					};
				for (const row of old
					.prepare("SELECT * FROM session WHERE account_id_type=?")
					.all(id))
					keys.sessions[`${row.address}.${row.device_id}`] = b64(row.record);
				for (const row of old.prepare("SELECT * FROM identity").all())
					keys.identities[row.address] = b64(row.identity_key);
				for (const row of old.prepare("SELECT * FROM sender_key").all()) {
					const distribution = serviceId(row.distribution_id);
					keys.senders[`${row.address}.${row.device_id}:${distribution}`] = b64(
						row.record,
					);
				}
			}
			for (const row of old.prepare("SELECT * FROM recipient").all())
				account.contacts.push({
					number: row.number,
					uuid: row.aci,
					name: row.given_name
						? [row.given_name, row.family_name].filter(Boolean).join(" ")
						: row.profile_given_name || row.number || row.aci,
					expiration: row.expiration_time,
				});
			for (const row of old.prepare("SELECT * FROM group_v2").all()) {
				const title = row.group_data ? field(row.group_data, 2) : undefined;
				account.groups.push({
					id: b64(row.group_id),
					name: title ? Buffer.from(title).toString("utf8") : b64(row.group_id),
					masterKey: b64(row.master_key),
					expiration: row.group_data
						? field(field(row.group_data, 4) || Buffer.alloc(0), 1, 0) || 0
						: 0,
					members: old
						.prepare(
							"SELECT r.aci FROM group_v2_member m JOIN recipient r ON r._id=m.recipient_id WHERE m.group_id=?",
						)
						.all(row._id)
						.map((r) => r.aci)
						.filter(Boolean),
					revision: row.group_data ? field(row.group_data, 6, 0) || 0 : 0,
				});
			}
			persist(db, account);
		} finally {
			old.close();
		}
	}
}

export function stores(account, kind = "aci") {
	const data = account.keys[kind];
	if (!data) throw new Error(`Missing ${kind.toUpperCase()} key material`);
	const addressKey = (address) => `${address.name()}.${address.deviceId()}`;
	const get = (table, id, Class) => {
		if (!table[id]) throw new Error(`Missing private key ${id}`);
		return Class.deserialize(unb64(table[id]));
	};
	const session = {
		async saveSession(address, record) {
			data.sessions[addressKey(address)] = b64(record.serialize());
		},
		async getSession(address) {
			const raw = data.sessions[addressKey(address)];
			return raw ? S.SessionRecord.deserialize(unb64(raw)) : null;
		},
		async getExistingSessions(addresses) {
			return Promise.all(
				addresses.map(async (address) => {
					const record = await this.getSession(address);
					if (!record) throw new Error("Missing Signal session");
					return record;
				}),
			);
		},
	};
	const identity = {
		async getIdentityKey() {
			return S.PrivateKey.deserialize(unb64(data.identity));
		},
		async getIdentityKeyPair() {
			const key = await this.getIdentityKey();
			return new S.IdentityKeyPair(key.getPublicKey(), key);
		},
		async getLocalRegistrationId() {
			return data.registrationId;
		},
		async saveIdentity(address, key) {
			const previous = data.identities[address.name()];
			const value = b64(key.serialize());
			if (previous && previous !== value)
				throw new Error(
					`Identity changed for ${address.name()}; verification required`,
				);
			data.identities[address.name()] = value;
			return S.IdentityChange.NewOrUnchanged;
		},
		async isTrustedIdentity(address, key) {
			return (
				!data.identities[address.name()] ||
				data.identities[address.name()] === b64(key.serialize())
			);
		},
		async getIdentity(address) {
			const value = data.identities[address.name()];
			return value ? S.PublicKey.deserialize(unb64(value)) : null;
		},
	};
	const prekey = {
		async getPreKey(id) {
			return get(data.prekeys, id, S.PreKeyRecord);
		},
		async savePreKey(id, record) {
			data.prekeys[id] = b64(record.serialize());
		},
		async removePreKey(id) {
			delete data.prekeys[id];
		},
	};
	const signed = {
		async getSignedPreKey(id) {
			return get(data.signed, id, S.SignedPreKeyRecord);
		},
		async saveSignedPreKey(id, record) {
			data.signed[id] = b64(record.serialize());
		},
	};
	const kyber = {
		async getKyberPreKey(id) {
			return get({ [id]: data.kyber[id]?.record }, id, S.KyberPreKeyRecord);
		},
		async saveKyberPreKey(id, record) {
			data.kyber[id] = { record: b64(record.serialize()), lastResort: false };
		},
		async markKyberPreKeyUsed(id, signedId, baseKey) {
			const token = `${id}:${signedId}:${b64(baseKey.serialize())}`;
			if (data.usedKyber[token])
				throw new Error("Reused Signal post-quantum prekey");
			data.usedKyber[token] = true;
			if (!data.kyber[id]?.lastResort) delete data.kyber[id];
		},
	};
	const sender = {
		async saveSenderKey(address, distribution, record) {
			data.senders[`${addressKey(address)}:${distribution}`] = b64(
				record.serialize(),
			);
		},
		async getSenderKey(address, distribution) {
			const raw = data.senders[`${addressKey(address)}:${distribution}`];
			return raw ? S.SenderKeyRecord.deserialize(unb64(raw)) : null;
		},
	};
	const local = S.ProtocolAddress.new(
		kind === "pni" ? account.pni : account.aci,
		account.deviceId,
	);
	return { session, identity, prekey, signed, kyber, sender, local };
}
