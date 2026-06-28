/**
 * YouTube OAuth2 Token Helper (Manual - for WSL2)
 *
 * Usage:
 *   node auth-manual.js
 *   - Copy the URL, open in browser, authorize
 *   - Copy the "code" from the redirect URL
 *   - Paste it when prompted
 */

import "dotenv/config";
import readline from "node:readline";

const CLIENT_ID = process.env.YOUTUBE_CLIENT_ID;
const CLIENT_SECRET = process.env.YOUTUBE_CLIENT_SECRET;
const REDIRECT_URI = "http://localhost:3458/callback";

if (!CLIENT_ID || !CLIENT_SECRET) {
  console.error("ERROR: Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first.");
  process.exit(1);
}

const SCOPES = [
  "https://www.googleapis.com/auth/youtube.readonly",
  "https://www.googleapis.com/auth/youtube.force-ssl",
].join(" ");

const authUrl = new URL("https://accounts.google.com/o/oauth2/v2/auth");
authUrl.searchParams.set("client_id", CLIENT_ID);
authUrl.searchParams.set("redirect_uri", REDIRECT_URI);
authUrl.searchParams.set("response_type", "code");
authUrl.searchParams.set("scope", SCOPES);
authUrl.searchParams.set("access_type", "offline");
authUrl.searchParams.set("prompt", "consent");

console.log("\n=== YouTube OAuth2 Setup (Manual) ===\n");
console.log("1. Open this URL in your browser:\n");
console.log(authUrl.toString());
console.log("\n2. Authorize the app");
console.log("3. You'll get an error page - that's OK!");
console.log("4. Look at the URL bar. Copy the 'code' parameter value.");
console.log("   Example: http://localhost:3458/callback?code=4/0ABC123...&scope=...");
console.log("   Copy only: 4/0ABC123...\n");

const rl = readline.createInterface({
  input: process.stdin,
  output: process.stdout,
});

rl.question("Paste the code here: ", async (code) => {
  rl.close();

  if (!code || code.trim() === "") {
    console.error("No code provided. Exiting.");
    process.exit(1);
  }

  code = code.trim();

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
      console.error(`\nERROR: ${tokens.error}`);
      console.error(tokens.error_description || "");
      process.exit(1);
    }

    console.log("\n=== SUCCESS! Add these to your .env file ===\n");
    console.log(`YOUTUBE_ACCESS_TOKEN=${tokens.access_token}`);
    if (tokens.refresh_token) {
      console.log(`YOUTUBE_REFRESH_TOKEN=${tokens.refresh_token}`);
    }
    console.log(`\nToken expires in ${tokens.expires_in} seconds.`);
    console.log("The refresh_token will auto-renew access tokens.\n");

  } catch (err) {
    console.error("Token exchange failed:", err.message);
    process.exit(1);
  }
});
