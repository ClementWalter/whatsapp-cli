/** Signal service protobufs and byte framing shared by the native client. */

import { fileURLToPath } from "node:url";
import {
	GroupMasterKey,
	GroupSecretParams,
} from "@signalapp/libsignal-client/zkgroup.js";
import protobuf from "protobufjs";

const root = await protobuf.load(
	["DeviceMessages", "DeviceName", "SignalService", "Groups", "Backups"].map(
		(name) => fileURLToPath(new URL(`./protos/${name}.proto`, import.meta.url)),
	),
);
export const type = (name) =>
	root.lookupType(name.includes(".") ? name : `signalservice.${name}`);
export const encode = (name, value) =>
	Buffer.from(type(name).encode(type(name).fromObject(value)).finish());
export const decode = (name, data) =>
	type(name).toObject(type(name).decode(data), {
		longs: Number,
		bytes: Buffer,
	});
export const b64 = (data) => Buffer.from(data).toString("base64");
export const unb64 = (data) => Buffer.from(data, "base64");

export function serviceId(binary, text) {
	if (!binary?.length) return text;
	const data = Buffer.from(binary);
	const hex = data.subarray(-16).toString("hex");
	const id = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
	return data.length === 17 && data[0] === 1 ? `PNI:${id}` : id;
}

export function groupId(masterKey) {
	return b64(
		GroupSecretParams.deriveFromMasterKey(new GroupMasterKey(masterKey))
			.getPublicParams()
			.getGroupIdentifier()
			.getContents(),
	);
}

export function pad(data) {
	const result = Buffer.alloc(Math.ceil((data.length + 2) / 80) * 80 - 1);
	Buffer.from(data).copy(result);
	result[data.length] = 0x80;
	return result;
}

export function unpad(data) {
	const result = Buffer.from(data);
	let end = result.length - 1;
	while (end >= 0 && result[end] === 0) end--;
	if (end < 0 || result[end] !== 0x80)
		throw new Error("Invalid Signal message padding");
	return result.subarray(0, end);
}

export function delimited(data) {
	const reader = protobuf.Reader.create(data);
	const frames = [];
	while (reader.pos < reader.len) {
		const length = reader.uint32();
		if (length > reader.len - reader.pos)
			throw new Error("Truncated Signal archive frame");
		frames.push(
			Buffer.from(reader.buf.subarray(reader.pos, reader.pos + length)),
		);
		reader.pos += length;
	}
	return frames;
}

export function field(data, number, wireType = 2) {
	const reader = protobuf.Reader.create(data);
	while (reader.pos < reader.len) {
		const tag = reader.uint32();
		if (tag >>> 3 === number && (tag & 7) === wireType)
			return wireType === 2 ? reader.bytes() : reader.uint32();
		reader.skipType(tag & 7);
	}
	return undefined;
}
