/**
 * Gmail OAuth2 Token Helper
 *
 * Run this ONCE to get your access_token and refresh_token.
 *
 * Prerequisites:
 *   1. Go to https://console.cloud.google.com/
 *   2. Enable Gmail API
 *   3. Create OAuth2 credentials (Web application)
 *   4. Add redirect URI: http://localhost:3457/callback
 *   5. Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env
 *
 * Usage:
 *   node auth.js
 */

import "dotenv/config";
import http from "node:http";
import { URL } from "node:url";

const CLIENT_ID = process.env.GMAIL_CLIENT_ID;
const CLIENT_SECRET = process.env.GMAIL_CLIENT_SECRET;
const REDIRECT_URI = "http://localhost:3457/callback";
const SCOPES = [
  "https://www.googleapis.com/auth/gmail.send",
  "https://www.googleapis.com/auth/gmail.readonly",
  "https://www.googleapis.com/auth/gmail.modify",
].join(" ");

if (!CLIENT_ID || !CLIENT_SECRET) {
  console.error("ERROR: Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.");
  process.exit(1);
}

const authUrl = new URL("https://accounts.google.com/o/oauth2/v2/auth");
authUrl.searchParams.set("client_id", CLIENT_ID);
authUrl.searchParams.set("redirect_uri", REDIRECT_URI);
authUrl.searchParams.set("response_type", "code");
authUrl.searchParams.set("scope", SCOPES);
authUrl.searchParams.set("access_type", "offline");
authUrl.searchParams.set("prompt", "consent");

console.log("\n=== Gmail OAuth2 Setup ===\n");
console.log("Open this URL in your browser:\n");
console.log(authUrl.toString());
console.log("\nWaiting for callback on http://localhost:3457/callback ...\n");

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  if (!url.pathname.startsWith("/callback")) {
    res.writeHead(404);
    res.end("Not found");
    return;
  }

  const code = url.searchParams.get("code");
  const error = url.searchParams.get("error");

  if (error) {
    res.writeHead(400, { "Content-Type": "text/html" });
    res.end(`<h1>Error</h1><p>${error}</p>`);
    server.close();
    process.exit(1);
  }

  if (!code) {
    res.writeHead(400, { "Content-Type": "text/html" });
    res.end("<h1>Error</h1><p>No authorization code received.</p>");
    return;
  }

  try {
    const tokenRes = await fetch("https://oauth2.googleapis.com/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        code,
        client_id: CLIENT_ID,
        client_secret: CLIENT_SECRET,
        redirect_uri: REDIRECT_URI,
        grant_type: "authorization_code",
      }),
    });

    const tokens = await tokenRes.json();

    if (tokens.error) {
      throw new Error(`${tokens.error}: ${tokens.error_description}`);
    }

    console.log("=== SUCCESS — Add these to your .env ===\n");
    console.log(`GMAIL_ACCESS_TOKEN=${tokens.access_token}`);
    if (tokens.refresh_token) {
      console.log(`GMAIL_REFRESH_TOKEN=${tokens.refresh_token}`);
    }
    console.log(`\nToken expires in ${tokens.expires_in} seconds.`);
    console.log("The refresh_token can be used to get new access tokens automatically.");

    res.writeHead(200, { "Content-Type": "text/html" });
    res.end("<h1>Success!</h1><p>Tokens printed in your terminal. You can close this tab.</p>");
  } catch (err) {
    console.error("Token exchange failed:", err.message);
    res.writeHead(500, { "Content-Type": "text/html" });
    res.end(`<h1>Error</h1><p>${err.message}</p>`);
  }

  server.close();
});

server.listen(3457);
