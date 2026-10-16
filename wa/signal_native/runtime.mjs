/** Repository-owned Signal client invoked by the Python command interface. */

import { setTimeout as delay } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import { importHistory } from "./history.mjs";
import { link } from "./link.mjs";
import { connect } from "./network.mjs";
import { drain } from "./receive.mjs";
import { send } from "./send.mjs";
import { accounts, database, importLegacy, select } from "./store.mjs";

export function argumentsFor(args) {
	const valueOptions = new Set([
		"--data-dir",
		"--account",
		"--output",
		"--name",
		"--timeout",
		"--attachment",
		"--group-id",
	]);
	const options = {};
	const positional = [];
	let terminated = false;
	for (let i = 0; i < args.length; i++) {
		const arg = args[i];
		if (arg === "--") {
			terminated = true;
			continue;
		}
		if (terminated) {
			positional.push(arg);
			continue;
		}
		if (arg.startsWith("--message=")) {
			options.message = arg.slice(10);
			continue;
		}
		if (valueOptions.has(arg)) {
			if (args[i + 1] === undefined)
				throw new Error(`Missing value for ${arg}`);
			options[arg.slice(2)] = args[++i];
		} else if (
			[
				"--no-history",
				"--ignore-attachments",
				"--ignore-stories",
				"--ignore-avatars",
				"--ignore-stickers",
				"--all-recipients",
			].includes(arg)
		)
			options[arg.slice(2)] = true;
		else if (arg.startsWith("-"))
			throw new Error(`Unknown native option ${arg}`);
		else positional.push(arg);
	}
	return { options, positional };
}

export async function execute(
	args,
	output = (value) => process.stdout.write(`${JSON.stringify(value)}\n`),
) {
	const { options, positional } = argumentsFor(args);
	if (!options["data-dir"])
		throw new Error("A native Signal data directory is required");
	const db = database(options["data-dir"]);
	try {
		importLegacy(db, options["data-dir"]);
		const operation = positional.shift();
		if (operation === "listAccounts")
			return output(
				accounts(db).map((a) => ({ number: a.number || null, aci: a.aci })),
			);
		if (operation === "link") {
			await link(
				db,
				options.name || "personal-cli",
				!options["no-history"],
				output,
			);
			return;
		}
		const account = select(db, options.account);
		if (operation === "listContacts") return output(account.contacts);
		if (operation === "listGroups")
			return output(account.groups.map((g) => ({ id: g.id, name: g.name })));
		if (!["receive", "send", "importHistory"].includes(operation))
			throw new Error(`Unsupported native Signal operation ${operation}`);
		const { chat, received } = await connect(db, account);
		try {
			if (operation === "receive") {
				const seconds = Number(options.timeout || 5);
				if (!Number.isInteger(seconds) || seconds < 1 || seconds > 60)
					throw new Error("Receive timeout must be between 1 and 60 seconds");
				await delay(seconds * 1000);
				const result = await drain(db, account);
				if (received.error) throw received.error;
				output({ received_events: received.count, ...result });
				if (result.pending)
					throw new Error(
						`${result.pending} encrypted messages remain queued for retry; no encrypted messages were discarded`,
					);
			} else if (operation === "send") {
				await drain(db, account);
				output(
					await send(
						db,
						account,
						chat,
						options["group-id"] || positional[0],
						options.message,
						options.attachment,
					),
				);
			} else output(await importHistory(db, account, chat));
		} finally {
			await chat.disconnect();
		}
	} finally {
		db.close();
	}
}

if (
	process.argv[1] &&
	import.meta.url === pathToFileURL(process.argv[1]).href
) {
	try {
		await execute(process.argv.slice(2));
	} catch (error) {
		console.error(error.message);
		process.exitCode = 1;
	}
}
