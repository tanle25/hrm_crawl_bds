/**
 * zalo_send.js — CLI for Zalo messaging via zca-js.
 *
 * Usage:
 *   node zalo_send.js login --cookie "<json>" --imei "<z_uuid>" --ua "<userAgent>"
 *   node zalo_send.js send <userId> <message>
 *   node zalo_send.js send-group <groupId> <message>
 *   node zalo_send.js status
 *   node zalo_send.js clear-session
 *   node zalo_send.js send-bulk <jsonFile>
 */
import { login, getApi, isLoggedIn, clearCredentials, getCredentialsMeta, findUserByPhone, listFriends, getAccountInfo, friendsSummary, forwardMessage } from "./cookie_manager.js";
import { ThreadType } from "zca-js";

const [,, command, ...args] = process.argv;

function parseArgs(args) {
  const params = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i].startsWith("--")) {
      const key = args[i];
      const next = args[i + 1];
      params[key] = next && !next.startsWith("--") ? next : true;
      if (next && !next.startsWith("--")) i++;
    } else {
      if (!params._) params._ = [];
      params._.push(args[i]);
    }
  }
  return params;
}

async function main() {
  switch (command) {
    case "login": {
      const opts = parseArgs(args);
      const creds = {};

      if (opts["--cookie"]) {
        try {
          creds.cookie = JSON.parse(opts["--cookie"]);
        } catch {
          creds.cookie = opts["--cookie"]; // treat as raw string
        }
      }
      if (opts["--imei"]) creds.imei = opts["--imei"];
      if (opts["--ua"]) creds.userAgent = opts["--ua"];

      const result = await login(creds);
      if (result.api) {
        console.log("[OK]");
      }
      break;
    }

    case "send": {
      const opts = parseArgs(args);
      const positional = opts._ || [];
      const [userId, ...msgParts] = positional;
      if (!userId || msgParts.length === 0) {
        console.error("Usage: node zalo_send.js send <userId> <message>");
        process.exit(1);
      }
      const api = await getApi();
      await api.sendMessage({ msg: msgParts.join(" ") }, userId, ThreadType.User);
      console.log("[OK]", userId);
      break;
    }

    case "send-group": {
      const opts = parseArgs(args);
      const positional = opts._ || [];
      const [groupId, ...msgParts] = positional;
      if (!groupId || msgParts.length === 0) {
        console.error("Usage: node zalo_send.js send-group <groupId> <message>");
        process.exit(1);
      }
      const api = await getApi();
      await api.sendMessage({ msg: msgParts.join(" ") }, groupId, ThreadType.Group);
      console.log("[OK]", groupId);
      break;
    }

    case "send-bulk": {
      const [jsonFile] = args;
      if (!jsonFile) {
        console.error("Usage: node zalo_send.js send-bulk <messages.json>");
        process.exit(1);
      }
      const { readFileSync } = await import("node:fs");
      const items = JSON.parse(readFileSync(jsonFile, "utf-8"));
      const api = await getApi();
      const delay = parseInt(process.env.ZALO_BULK_DELAY_S || "5", 10);

      for (const item of items) {
        const type = item.type === "Group" ? ThreadType.Group : ThreadType.User;
        const uid = item.userId || item.user_id || item.zalo_id || "";
        const msg = item.msg || item.message || "";
        if (!uid || !msg) { console.warn("[SKIP] invalid item:", item); continue; }
        try {
          await api.sendMessage({ msg }, uid, type);
          console.log("[OK]", uid);
        } catch (err) {
          console.error("[FAIL]", uid, err.message);
        }
        await new Promise(r => setTimeout(r, delay));
      }
      console.log("[DONE]");
      break;
    }

    case "status": {
      const meta = getCredentialsMeta();
      const loggedIn = isLoggedIn();
      console.log(JSON.stringify({
        logged_in: loggedIn,
        credentials: meta,
      }));
      break;
    }

    case "clear-session": {
      clearCredentials();
      console.log("[OK]");
      break;
    }

    case "find-user": {
      const [phoneOrKeyword] = args;
      if (!phoneOrKeyword) {
        console.log(JSON.stringify({ error: "Usage: find-user <phone>" }));
        process.exit(1);
      }
      try {
        const user = await findUserByPhone(phoneOrKeyword);
        console.log(JSON.stringify(user));
      } catch (err) {
        console.log(JSON.stringify({ error: err.message }));
        process.exit(1);
      }
      break;
    }

    case "list-friends": {
      try {
        const friends = await listFriends();
        console.log(JSON.stringify(friends));
      } catch (err) {
        console.log(JSON.stringify({ error: err.message }));
        process.exit(1);
      }
      break;
    }

    case "count-friends": {
      try {
        const friends = await listFriends();
        console.log(JSON.stringify({ total: friends.length }));
      } catch (err) {
        console.log(JSON.stringify({ error: err.message }));
        process.exit(1);
      }
      break;
    }

    case "account-info": {
      try {
        const info = await getAccountInfo();
        console.log(JSON.stringify(info));
      } catch (err) {
        console.log(JSON.stringify({ error: err.message }));
        process.exit(1);
      }
      break;
    }

    case "friends-summary": {
      try {
        const summary = await friendsSummary();
        console.log(JSON.stringify(summary));
      } catch (err) {
        console.log(JSON.stringify({ error: err.message }));
        process.exit(1);
      }
      break;
    }

    case "forward": {
      // Usage: node zalo_send.js forward <jsonFile>
      // jsonFile: { message: "...", userIds: ["id1", ...], reference?: { id, ts } }
      const [jsonFile] = args;
      if (!jsonFile) {
        console.log(JSON.stringify({ error: "Usage: node zalo_send.js forward <forward.json>" }));
        process.exit(1);
      }
      try {
        const { readFileSync } = await import("node:fs");
        const payload = JSON.parse(readFileSync(jsonFile, "utf-8"));
        const { message, userIds, reference } = payload;
        if (!userIds?.length) {
          console.log(JSON.stringify({ error: "userIds trong." }));
          process.exit(1);
        }
        // Split into batches of 100
        const batchSize = 100;
        const results = { ok: 0, fail: 0, total: userIds.length, batches: [] };
        for (let i = 0; i < userIds.length; i += batchSize) {
          const batch = userIds.slice(i, i + batchSize);
          const r = await forwardMessage({ message, userIds: batch, reference });
          results.ok += r.ok;
          results.fail += r.fail;
          results.batches.push({ batch: i / batchSize + 1, ...r });
          await new Promise(r => setTimeout(r, 2000));
        }
        console.log(JSON.stringify(results));
      } catch (err) {
        console.log(JSON.stringify({ error: err.message }));
        process.exit(1);
      }
      break;
    }

    default:
      console.log("Commands: login, send, send-group, send-bulk, status, clear-session");
      process.exit(1);
  }
}

main().catch(err => {
  console.error("[ERROR]", err.message);
  process.exit(1);
});
