/**
 * LinkedIn OAuth2 Token Helper
 *
 * Run this ONCE to get your access_token and refresh_token.
 * It starts a local server, opens the LinkedIn authorization page,
 * exchanges the code for tokens, and prints them for your .env file.
 *
 * Prerequisites:
 *   1. Create a LinkedIn App at https://developer.linkedin.com/
 *   2. Request "Share on LinkedIn" product (grants w_member_social scope)
 *   3. Add redirect URI: http://localhost:3456/callback
 *   4. Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in .env
 *
 * Usage:
 *   node auth.js
 */

import "dotenv/config";
import http from "node:http";
import { URL } from "node:url";

const CLIENT_ID = process.env.LINKEDIN_CLIENT_ID;
const CLIENT_SECRET = process.env.LINKEDIN_CLIENT_SECRET;
const REDIRECT_URI = "http://localhost:3456/callback";
const SCOPES = "openid profile w_member_social";

if (!CLIENT_ID || !CLIENT_SECRET) {
  console.error("ERROR: Set LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in .env first.");
  process.exit(1);
}

// Step 1 — build the authorization URL
const authUrl = new URL("https://www.linkedin.com/oauth/v2/authorization");
authUrl.searchParams.set("response_type", "code");
authUrl.searchParams.set("client_id", CLIENT_ID);
authUrl.searchParams.set("redirect_uri", REDIRECT_URI);
authUrl.searchParams.set("scope", SCOPES);
authUrl.searchParams.set("state", crypto.randomUUID());

console.log("\n=== LinkedIn OAuth2 Setup ===\n");
console.log("Open this URL in your browser:\n");
console.log(authUrl.toString());
console.log("\nWaiting for callback on http://localhost:3456/callback ...\n");

// Step 2 — start a local server to capture the callback
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
    res.end(`<h1>Error</h1><p>${error}: ${url.searchParams.get("error_description")}</p>`);
    server.close();
    process.exit(1);
  }

  if (!code) {
    res.writeHead(400, { "Content-Type": "text/html" });
    res.end("<h1>Error</h1><p>No authorization code received.</p>");
    return;
  }

  // Step 3 — exchange code for tokens
  try {
    const tokenRes = await fetch("https://www.linkedin.com/oauth/v2/accessToken", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code,
        redirect_uri: REDIRECT_URI,
        client_id: CLIENT_ID,
        client_secret: CLIENT_SECRET,
      }),
    });

    const tokens = await tokenRes.json();

    if (tokens.error) {
      throw new Error(`${tokens.error}: ${tokens.error_description}`);
    }

    console.log("=== SUCCESS — Add these to your .env ===\n");
    console.log(`LINKEDIN_ACCESS_TOKEN=${tokens.access_token}`);
    if (tokens.refresh_token) {
      console.log(`LINKEDIN_REFRESH_TOKEN=${tokens.refresh_token}`);
    }
    console.log(`\nToken expires in ${tokens.expires_in} seconds.`);

    res.writeHead(200, { "Content-Type": "text/html" });
    res.end("<h1>Success!</h1><p>Tokens printed in your terminal. You can close this tab.</p>");
  } catch (err) {
    console.error("Token exchange failed:", err.message);
    res.writeHead(500, { "Content-Type": "text/html" });
    res.end(`<h1>Error</h1><p>${err.message}</p>`);
  }

  server.close();
});

server.listen(3456);
