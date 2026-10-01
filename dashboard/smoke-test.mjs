import { chromium } from "playwright";
import fs from "node:fs";

const SHOT_DIR = "C:/Users/iavit/AppData/Local/Temp/claude/c--Users-iavit-OneDrive-Documents-undertow/5363b383-8a66-4a5a-b98e-112a1e53c45b/scratchpad";
fs.mkdirSync(SHOT_DIR, { recursive: true });

const consoleErrors = [];
const networkErrors = [];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });

page.on("console", (msg) => {
  if (msg.type() === "error") consoleErrors.push(msg.text());
});
page.on("requestfailed", (req) => {
  networkErrors.push(`${req.method()} ${req.url()} -> ${req.failure()?.errorText}`);
});
page.on("response", (res) => {
  if (res.status() >= 400) networkErrors.push(`${res.status()} ${res.url()}`);
});

console.log("navigating to signals view...");
await page.goto("http://localhost:5173", { waitUntil: "domcontentloaded" });
await page.waitForSelector("text=Flagged events", { timeout: 15000 });
await page.waitForTimeout(4000);
await page.screenshot({ path: `${SHOT_DIR}/01-signals.png`, fullPage: true });
console.log("screenshot: 01-signals.png");

console.log("clicking BTC filter...");
await page.click("text=BTC");
await page.waitForTimeout(1000);
await page.screenshot({ path: `${SHOT_DIR}/02-signals-btc.png`, fullPage: true });
console.log("screenshot: 02-signals-btc.png");

console.log("switching to wallets view...");
await page.click("text=WALLETS");
await page.waitForSelector("text=Notable wallets", { timeout: 15000 });
await page.waitForTimeout(20000);
await page.screenshot({ path: `${SHOT_DIR}/03-wallets.png`, fullPage: true });
console.log("screenshot: 03-wallets.png");

console.log("switching to catalysts view...");
await page.click("text=CATALYSTS");
await page.waitForSelector("text=Trump-announcement catalysts", { timeout: 15000 });
await page.waitForTimeout(2000);
await page.screenshot({ path: `${SHOT_DIR}/04-catalysts.png`, fullPage: true });
console.log("screenshot: 04-catalysts.png");

console.log("clicking tariff_crash catalyst...");
await page.click("text=China tariff announcement crash");
await page.waitForTimeout(8000);
await page.screenshot({ path: `${SHOT_DIR}/05-catalysts-crash.png`, fullPage: true });
console.log("screenshot: 05-catalysts-crash.png");

console.log("expanding a related transaction...");
await page.locator("button", { hasText: "BTC PUT 100,000" }).first().click();
await page.waitForTimeout(1500);
await page.screenshot({ path: `${SHOT_DIR}/06-catalysts-expanded.png`, fullPage: true });
console.log("screenshot: 06-catalysts-expanded.png");

console.log("switching to leaderboard view...");
await page.click("text=LEADERBOARD");
await page.waitForSelector("text=Top winning wallets", { timeout: 15000 });
await page.waitForTimeout(2000);
await page.screenshot({ path: `${SHOT_DIR}/08-leaderboard.png`, fullPage: true });
console.log("screenshot: 08-leaderboard.png");

console.log("deep-diving from leaderboard...");
await page.locator("button", { hasText: "deep dive" }).first().click();
await page.waitForTimeout(4000);
await page.screenshot({ path: `${SHOT_DIR}/09-leaderboard-deepdive.png`, fullPage: true });
console.log("screenshot: 09-leaderboard-deepdive.png");

await browser.close();

console.log("\n--- console errors ---");
console.log(consoleErrors.length ? consoleErrors.join("\n") : "(none)");
console.log("\n--- network errors ---");
console.log(networkErrors.length ? networkErrors.join("\n") : "(none)");
