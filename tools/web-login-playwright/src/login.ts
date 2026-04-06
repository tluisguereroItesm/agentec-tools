import { chromium } from "playwright";
import { LoginInput } from "./types";

export async function executeLogin(input: LoginInput, screenshotPath: string) {
  const browser = await chromium.launch({
    headless: input.headless ?? true
  });

  const page = await browser.newPage();
  page.setDefaultTimeout(input.timeoutMs ?? 30000);

  try {
    await page.goto(input.url, { waitUntil: "domcontentloaded" });
    await page.fill(input.usernameSelector, input.username);
    await page.fill(input.passwordSelector, input.password);
    await page.click(input.submitSelector);

    if (input.successIndicator) {
      await page.waitForSelector(input.successIndicator, { timeout: input.timeoutMs ?? 30000 });
    }

    await page.screenshot({ path: screenshotPath, fullPage: true });

    await browser.close();
    return { success: true, message: "Login ejecutado correctamente" };
  } catch (error) {
    await page.screenshot({ path: screenshotPath, fullPage: true });
    await browser.close();
    return {
      success: false,
      message: error instanceof Error ? error.message : "Error desconocido en login"
    };
  }
}