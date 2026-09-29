async (page, baseUrl = "http://127.0.0.1:8765") => {
  const failures = [];
  let checks = 0;
  const check = (condition, name) => {
    checks += 1;
    if (!condition) failures.push(name);
  };
  const requests = [];
  const errors = [];
  let saveError = null;
  page.on("pageerror", error => errors.push(error.message));
  page.on("dialog", dialog => dialog.accept().catch(() => {}));
  await page.addInitScript(() => {
    window.confirm = () => true;
    window.__smsAlerts = [];
    window.alert = message => window.__smsAlerts.push(String(message));
  });
  const defaults = {
    fiveSimApiKeyPresent: true, fiveSimCountry: "vietnam", fiveSimOperator: "custom",
    fiveSimProduct: "openai", fiveSimMaxPrice: "0.5", fiveSimCandidateLimit: "4",
    fiveSimAcquirePriority: "price", emailSource: "manual", codeSource: "manual", authMode: "otp",
    fiveSimUseProxy: false,
  };
  const account = {id: 7, email: "fixture@example.com", hasAccessToken: true,
    codexPhoneNumberMasked: "••••0001", localValidity: {status: "unexpired"}};
  const pool = [{phone: "+15550000001", country: "usa", operator: "any", product: "openai",
    successful_uses: 0, max_uses: 3, remaining: 3, usable: true}];
  const order = {id: 1, phone: pool[0].phone, status: "PENDING", country: "usa", operator: "any", price: 0.1};
  await page.unroute("**/api/**");
  await page.route("**/api/**", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const body = request.postDataJSON();
    requests.push({path, body});
    let result = {ok: true, running: false, results: []};
    if (path === "/api/defaults") {
      if (request.method() === "POST") {
        if (saveError) {
          await route.fulfill({status: 422, json: {detail: saveError}});
          return;
        }
        Object.assign(defaults, body);
        delete defaults.fiveSimApiKey;
        result = {ok: true, fiveSimApiKeyPresent: true};
      } else result = defaults;
    } else if (path === "/api/accounts") result = [account];
    else if (path === "/api/5sim/profile") result = {ok: true, profile: {balance: 12, currency: "USD"}};
    else if (path === "/api/5sim/pool") result = {ok: true, pool, total: pool.length};
    else if (path === "/api/5sim/prices") result = {ok: true, entries: [
      {country: "vietnam", operator: "custom", cost: 0.1, count: 10, rate: 90, rateStr: "90%"},
      {country: "usa", operator: "any", cost: 0.2, count: 20, rate: 80, rateStr: "80%"},
    ], total: 2, countries: ["vietnam", "usa"]};
    else if (path === "/api/5sim/buy" || path === "/api/5sim/reuse") result = {ok: true, order};
    else if (path.startsWith("/api/5sim/check/")) result = {ok: true, order: {...order, status: "RECEIVED", code: "654321"}};
    else if (path === "/api/batch/stream") {
      await route.fulfill({contentType: "text/event-stream", body: "data: {}\n\n"});
      return;
    }
    await route.fulfill({json: result});
  });
  const waitRequest = async (path, action) => {
    const response = page.waitForResponse(response => new URL(response.url()).pathname === path);
    await action();
    await response;
  };
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto(baseUrl + "/");
  await page.locator("#fsApiKeyStatus").filter({hasText: "已配置"}).waitFor({state: "attached"});
  await page.locator("#nav-sms").click();
  check(await page.locator("#view-sms #fivesimPanel").isVisible(), "SMS panel in its own view");
  check(await page.locator("#view-tools #fivesimPanel").count() === 0, "tools panel retired");
  check(await page.locator("#fsApiKey").inputValue() === "", "saved key never echoed");
  check(await page.locator("#fsCountryFilter").inputValue() === "vietnam", "saved country restored");
  check(await page.locator("#fsOperatorFilter").inputValue() === "custom", "custom operator restored");
  check(!await page.locator("#fsUseProxy").isChecked(), "SMS direct connection restored");
  await waitRequest("/api/5sim/profile", () => page.locator("#fsProfileBtn").click());
  check(requests.findLast(request => request.path === "/api/5sim/profile").body.apiKey === "", "profile uses saved key");
  await waitRequest("/api/5sim/prices", () => page.locator("#fsQueryBtn").click());
  await page.locator("#fsCountryFilter").selectOption("usa");
  await page.locator("#fsOperatorFilter").selectOption("any");
  saveError = [{type: "extra_forbidden", loc: ["body", "fiveSimCountry"]}];
  await waitRequest("/api/defaults", () => page.locator("#fsSaveKey").click());
  await page.waitForFunction(() => window.__smsAlerts.some(message => message.includes("旧版短信设置接口")));
  check(await page.evaluate(() => window.__smsAlerts.at(-1).includes("重启 WebUI")), "outdated backend has actionable message");
  saveError = [{type: "int_from_float", loc: ["body", "fiveSimCandidateLimit"]}];
  await waitRequest("/api/defaults", () => page.locator("#fsSaveKey").click());
  await page.waitForFunction(() => window.__smsAlerts.some(message => message.includes("候选库存组合上限格式不正确")));
  check(await page.evaluate(() => window.__smsAlerts.at(-1).includes("候选库存组合上限格式不正确")), "validation identifies the field");
  saveError = null;
  await waitRequest("/api/defaults", () => page.locator("#fsSaveKey").click());
  const saved = requests.findLast(request => request.path === "/api/defaults" && request.body);
  check(saved.body.fiveSimCountry === "usa", "selected country saved");
  check(saved.body.fiveSimUseProxy === false, "SMS connection choice saved");
  check(!("fiveSimApiKey" in saved.body), "blank key preserved");
  await page.locator("#fsApiKey").fill("fixture-new-key");
  await waitRequest("/api/defaults", () => page.locator("#fsSaveKey").click());
  await page.waitForFunction(() => document.getElementById("fsApiKey").value === "");
  check(await page.locator("#fsApiKey").inputValue() === "", "key cleared after save");
  await waitRequest("/api/5sim/buy", () => page.locator("#fsPriceBody button").first().click());
  await page.locator("#fsActiveOrder").waitFor({state: "visible"});
  await waitRequest("/api/5sim/check/1", () => page.locator("#fsCheckBtn").click());
  await waitRequest("/api/5sim/finish/1", () => page.locator("#fsFinishBtn").click());
  await page.locator("#fsPoolSummary").click();
  await waitRequest("/api/5sim/reuse", () => page.locator("#fsPoolBody button.primary").first().click());
  const reused = requests.findLast(request => request.path === "/api/5sim/reuse");
  check(reused.body.phone === pool[0].phone, "reuse requests the clicked phone");
  await waitRequest("/api/5sim/cancel/1", () => page.locator("#fsCancelBtn").click());
  check(requests.filter(request => /^\/api\/5sim\/(buy|check|finish|reuse|cancel)/.test(request.path))
    .every(request => request.body.apiKey === ""), "all order actions use saved key");
  await page.reload();
  await page.waitForFunction(() => document.getElementById("fsCountryFilter").value === "usa");
  check(await page.locator("#fsCountryFilter").inputValue() === "usa", "country survives reload");
  await page.locator("#nav-accounts").click();
  await page.locator("#accountList").filter({hasText: "fixture@example.com"}).waitFor();
  check((await page.locator("#accountList").innerText()).includes("••••0001"), "masked phone visible");
  check(await page.locator("#view-accounts th").count() === 15, "account table column count");
  await page.locator("#nav-sms").click();
  await page.locator("#fsPoolSummary").click();
  await page.screenshot({path: "output/playwright/sms-desktop.png", fullPage: true});
  await page.setViewportSize({width: 390, height: 844});
  check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "mobile page stays in viewport");
  check(await page.locator("#fsApiKey").isVisible(), "mobile key input visible");
  await page.screenshot({path: "output/playwright/sms-mobile.png", fullPage: true});
  check(errors.length === 0, `no browser exceptions: ${errors.join(", ")}`);
  check(!JSON.stringify(defaults).includes("fixture-new-key"), "mock defaults never disclose key");
  if (failures.length) throw new Error(failures.join("; "));
  return {checks, failures, mockedRequests: requests.length};
}
