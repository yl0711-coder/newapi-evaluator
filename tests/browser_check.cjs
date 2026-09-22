const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const { pathToFileURL } = require('url');

async function main() {
  const root = process.argv[2];
  const modulePath = process.env.PLAYWRIGHT_MODULE;
  if (!modulePath) throw new Error('PLAYWRIGHT_MODULE is required');
  const { chromium } = require(modulePath);
  const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge', headless: true});
  let checks = 0;
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.name));
    await page.route('http://**', route => route.abort());
    await page.route('https://**', route => route.abort());
    await page.setViewportSize({width: 1440, height: 1000});
    await page.goto(pathToFileURL(path.join(root, 'report.html')).href);
    assert.equal(await page.title(), '小时渠道诊断'); checks++;
    assert.equal(await page.locator('#control-start').count(), 1); checks++;
    assert.equal(await page.locator('#control-enable-start').count(), 1); checks++;
    assert.equal(await page.locator('#control-run-once').count(), 1); checks++;
    assert.equal(await page.locator('#control-stop').count(), 1); checks++;
    assert.equal(await page.locator('#details tbody tr').count(), 22); checks++;
    assert.equal(await page.locator('#overview tbody tr').count(), 4); checks++;
    assert.equal(await page.locator('#overview tbody tr td:nth-child(3)').first().innerText(), '0.4'); checks++;
    assert.ok(await page.locator('#details tbody').innerText().then(text => text.includes('0.4×'))); checks++;
    assert.equal(await page.locator('#details tbody tr').filter({hasText:'verified'}).count(), 10); checks++;
    assert.equal(await page.locator('#details tbody tr').filter({hasText:'不适用 / 未返回'}).count(), 6); checks++;
    assert.ok(await page.locator('svg circle').count() > 0); checks++;
    const detail = page.locator('details').first();
    await detail.locator('summary').click();
    assert.equal(await detail.getAttribute('open'), null); checks++;
    await detail.locator('summary').click();
    await page.screenshot({path:path.join(root,'desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),true); checks++;
    assert.ok(await page.locator('.scroll').evaluateAll(nodes => nodes.some(node => node.scrollWidth > node.clientWidth))); checks++;
    await page.screenshot({path:path.join(root,'mobile.png'),fullPage:true});
    assert.equal(errors.length,0); checks++;
    fs.writeFileSync(path.join(root,'browser.json'),JSON.stringify({status:'passed',checks},null,2));
  } finally {await browser.close();}
}
main().catch(error => {process.stderr.write(`${error.name}: ${error.message}\n`);process.exitCode=1;});
