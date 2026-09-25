const readline = require('readline');
const {chromium} = require('playwright');

let browser, page;
async function observation() {
  const state = await page.evaluate(() => window.sceneqaAgent.snapshot());
  const image = await page.locator('#scene').screenshot({type: 'png'});
  return {...state, image: 'data:image/png;base64,' + image.toString('base64')};
}
async function handle(command) {
  if (command.op === 'init') {
    if (browser) throw new Error('already_initialized');
    browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader']});
    page = await browser.newPage({viewport: {width: 1280, height: 900}, deviceScaleFactor: 1});
    await page.goto(command.url, {waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => window.sceneqaAgent?.snapshot().ready, {timeout: 60000});
    return {observation: await observation()};
  }
  if (command.op === 'act') {
    if (!page) throw new Error('not_initialized');
    const allowed = new Set(['focus_at', 'orbit', 'zoom', 'move', 'undo_view']);
    if (!allowed.has(command.name)) return {result: {ok: false, error: 'unknown_tool'}, observation: await observation()};
    const result = await page.evaluate(async ({name, args}) => window.sceneqaAgent.act(name, args),
                                       {name: command.name, args: command.args});
    return {result, observation: await observation()};
  }
  if (command.op === 'trace') {
    if (!page) throw new Error('not_initialized');
    return page.evaluate(() => window.sceneqaAgent.trace());
  }
  if (command.op === 'close') {
    if (browser) await browser.close();
    browser = undefined;
    return {closed: true};
  }
  throw new Error('unknown_operation');
}

const input = readline.createInterface({input: process.stdin});
let queue = Promise.resolve();
input.on('line', line => {
  queue = queue.then(async () => {
    let response;
    try { response = {ok: true, value: await handle(JSON.parse(line))}; }
    catch (error) { response = {ok: false, error: String(error?.message || error)}; }
    process.stdout.write(JSON.stringify(response) + '\n');
  }).catch(error => process.stderr.write(String(error) + '\n'));
});
input.on('close', () => queue.then(async () => {if (browser) await browser.close()}));
