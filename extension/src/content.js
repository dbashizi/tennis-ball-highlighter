// Classic content script (MV3 content scripts can't be ES modules). It only
// loads the real module, which is listed in web_accessible_resources.
(async () => {
  try {
    const mod = await import(chrome.runtime.getURL("src/content-main.js"));
    mod.main();
  } catch (err) {
    console.warn("[tennis-ball-highlighter] failed to start:", err);
  }
})();
