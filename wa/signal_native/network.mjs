/** Direct Signal service connections with a durable encrypted receive queue. */
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import https from "node:https";
import { rootCertificates } from "node:tls";
import * as S from "@signalapp/libsignal-client";
import { decode } from "./protocol.mjs";

const certificateAuthorities = [
	...rootCertificates,
	fs.readFileSync(new URL("./signal-ca.pem", import.meta.url), "utf8"),
];

export function cdnRequest(
	target,
	{ method = "GET", headers = {}, body, signal } = {},
) {
	const url = new URL(target);
	if (url.protocol !== "https:")
		throw new Error("Signal CDN requests require HTTPS");
	return new Promise((resolve, reject) => {
		const outgoing = https.request(
			url,
			{
				method,
				headers,
				signal,
				ca: certificateAuthorities,
				rejectUnauthorized: true,
			},
			(incoming) => {
				resolve({
					status: incoming.statusCode,
					ok: incoming.statusCode >= 200 && incoming.statusCode < 300,
					body: incoming,
					headers: {
						get(name) {
							const value = incoming.headers[name.toLowerCase()];
							return Array.isArray(value) ? value[0] : value || null;
						},
					},
				});
			},
		);
		outgoing.on("error", reject);
		outgoing.end(body);
	});
}

export async function serviceRequest(verb, path, body, headers = {}) {
	const response = await cdnRequest(new URL(path, "https://chat.signal.org"), {
		method: verb,
		headers: {
			"Content-Type": "application/json",
			"User-Agent": "whatsapp-cli/0.1.0",
			...headers,
		},
		body: Buffer.from(JSON.stringify(body)),
		signal: AbortSignal.timeout(60000),
	});
	const chunks = [];
	let length = 0;
	for await (const chunk of response.body) {
		length += chunk.length;
		if (length > 1024 * 1024)
			throw new Error("Signal service response exceeds 1 MiB");
		chunks.push(chunk);
	}
	if (!response.ok)
		throw new Error(`Signal ${verb} ${path} returned HTTP ${response.status}`);
	const data = Buffer.concat(chunks);
	return {
		status: response.status,
		body: data.length ? JSON.parse(data.toString("utf8")) : null,
	};
}

export function network() {
	return new S.Net.Net({
		env: S.Net.Environment.Production,
		userAgent: "whatsapp-cli/0.1.0",
	});
}

export async function connect(db, account) {
	const received = { count: 0, empty: false, error: null };
	const chat = await network().connectAuthenticatedChat(
		`${account.aci}.${account.deviceId}`,
		account.password,
		false,
		{
			onIncomingMessage(envelope, timestamp, ack) {
				try {
					const decoded = decode("Envelope", envelope);
					const id =
						decoded.serverGuid ||
						(decoded.serverGuidBinary?.length
							? Buffer.from(decoded.serverGuidBinary).toString("hex")
							: randomUUID());
					// Acknowledgement follows a committed encrypted copy, so decrypt failures remain retryable.
					if (
						!db
							.prepare("SELECT 1 FROM native_seen WHERE account=? AND id=?")
							.get(account.aci, id)
					)
						db.prepare(
							"INSERT OR IGNORE INTO native_queue VALUES (?,?,?,?)",
						).run(account.aci, id, envelope, timestamp);
					ack.send(200);
					received.count++;
				} catch (error) {
					received.error = error;
				}
			},
			onQueueEmpty() {
				received.empty = true;
			},
			onConnectionInterrupted(error) {
				if (error) received.error = error;
			},
		},
		{ abortSignal: AbortSignal.timeout(30000) },
	);
	return { chat, received };
}

export async function request(
	chat,
	verb,
	path,
	body,
	allowed = [],
	headers = [],
) {
	const response = await chat.fetch({
		verb,
		path,
		headers: [
			...headers,
			...(body ? [["Content-Type", "application/json"]] : []),
		],
		body: body ? Buffer.from(JSON.stringify(body)) : undefined,
		timeoutMillis: 60000,
	});
	if (
		(response.status < 200 || response.status >= 300) &&
		!allowed.includes(response.status)
	) {
		throw new Error(
			`Signal ${verb} ${path.split("?")[0]} returned HTTP ${response.status}`,
		);
	}
	return {
		status: response.status,
		body: response.body?.length
			? JSON.parse(Buffer.from(response.body).toString("utf8"))
			: null,
	};
}
