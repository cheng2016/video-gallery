from pathlib import Path
import json
import subprocess
import unittest


def card_refresh_fixture_script():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    functions = []
    for start, end in [
        ("function videoLocationKey(", "function pruneDeletedVideos("),
        ("function pruneDeletedVideos(", "function applyDurationBadgesFromLoaded("),
        ("function mergeLoadedVideoMeta(", "function syncCardDurationBadge("),
        ("async function refreshLoadedCardMeta(", "function cardHtml("),
        ("async function loadMore(", "function syncLanShareUi("),
    ]:
        functions.append(start + html.split(start, 1)[1].split(end, 1)[0])
    return "\n".join(functions) + r'''
let state, refreshToken, loadingMore = false, loadMoreCallCount = 0, loadMoreAbortCount = 0;
const LOADED_META_FIELDS = ["duration_h", "has_thumb", "bad"];
const logs = [], checks = [];
const browser = typeof document !== "undefined";
const scroller = browser ? document.getElementById("contentScroll") : {
  scrollTop: 120, scrollHeight: 960, clientHeight: 240, style: { overflowAnchor: "auto" }
};
const empty = browser ? document.getElementById("empty") : {style: {}};
const hint = browser ? document.getElementById("loadMoreHint") : {};
let cards = [];
const grid = browser ? document.getElementById("grid") : {
  querySelectorAll(selector) { return selector === ".card" ? cards : []; }, innerHTML: ""
};
function $(id) { return {grid, contentScroll: scroller, empty, loadMoreHint: hint}[id]; }
function libCacheKey(root) { return String(root || "").replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase(); }
function sameLibPath(a, b) { return !!a && libCacheKey(a) === libCacheKey(b); }
function cancelListRequests() {}
function updateGridHint() {}
function renderVirtualGrid() {}
function applyCardBadgesFromLoaded() {}
function summarizeGridCardCoverage() {}
function clientNow() { return performance.now(); }
function clientElapsedMs(start) { return performance.now() - start; }
function clientLog(...args) { logs.push(args); }
function isAbortError() { return false; }
function renderGrid(batch) { state.loadedVideos.push(...batch); }
function check(condition, label) { if (!condition) throw new Error(label); checks.push(label); }
function reset() {
  refreshToken = 1;
  const rows = Array.from({length: 6}, (_, i) => ({
    id: i ? "current-" + i : "old-id", root: "C:\\", rel: "movie-" + i + ".mp4",
    size: 100, mtime: 10, kind: "", sourceVersion: JSON.stringify([100, 10, ""])
  }));
  state = {loadedVideos: rows, _libGen: 23, loadedBaseOffset: 0, offset: 6,
    totalCount: 5, rawCount: 5, hasMore: false, virtualEnabled: false};
  if (browser) {
    grid.innerHTML = rows.map(v => '<div class="card" style="height:160px"></div>').join("");
    Array.from(grid.children).forEach((card, i) => Object.assign(card.dataset, rows[i]));
  } else {
    cards = rows.map(v => ({dataset: {...v}, remove() { cards = cards.filter(c => c !== this); }}));
  }
  scroller.scrollTop = 120;
  scroller.style.overflowAnchor = "auto";
  globalThis.fetch = async () => ({json: async () => ({
    videos: rows.slice(1).map(v => ({...v, duration_h: "00:42"})),
    missing: [{id: "old-id", root: "C:\\", confirmed: true}],
    catalog_stable: true, lib_gen: 23
  })});
}
async function runCardRefreshChecks() {
  reset();
  await refreshLoadedCardMeta("test_stable");
  check(state.loadedVideos.length === 5 && !state.loadedVideos.some(v => v.id === "old-id"), "stable_missing_pruned");
  check(grid.querySelectorAll(".card").length === 5, "only_stale_dom_removed");
  check(scroller.scrollTop === 120, "scroll_preserved");
  check(scroller.style.overflowAnchor === "auto", "anchor_restored");
  check(state.totalCount === 5 && state.rawCount === 5 && state.offset === 5, "counts_not_decremented_twice");
  check(state.loadedVideos.every(v => v.duration_h === "00:42"), "other_card_metadata_updated");
  for (const mode of ["unconfirmed", "busy", "offline", "server_gen", "client_gen", "token", "size", "mtime", "kind", "segments", "rel", "root", "new_row"]) {
    reset();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async () => {
      const data = await (await originalFetch()).json();
      if (mode === "unconfirmed") data.missing[0].confirmed = false;
      if (mode === "busy") data.catalog_stable = false;
      if (mode === "offline") data.missing[0].offline = true;
      if (mode === "server_gen") data.lib_gen = 24;
      if (mode === "client_gen") state._libGen = 24;
      if (mode === "token") refreshToken++;
      if (mode === "size") state.loadedVideos[0].size = 101;
      if (mode === "mtime") state.loadedVideos[0].mtime = 11;
      if (mode === "kind") state.loadedVideos[0].kind = "series";
      if (mode === "segments") state.loadedVideos[0].segments = ["new.ts"];
      if (mode === "rel") state.loadedVideos[0].rel = "replacement.mp4";
      if (mode === "root") state.loadedVideos[0].root = "D:\\";
      if (mode === "new_row") data.videos.push({...state.loadedVideos[0], duration_h: "00:42"});
      return {json: async () => data};
    };
    await refreshLoadedCardMeta(mode);
    check(state.loadedVideos.length === 6, mode + "_kept");
  }
  reset();
  const replacement = {...state.loadedVideos[0], id: "replacement-id", size: 101,
    sourceVersion: JSON.stringify([101, 10, ""])};
  state.loadedVideos.push(replacement);
  if (browser) {
    const card = document.createElement("div");
    card.className = "card";
    Object.assign(card.dataset, replacement);
    grid.appendChild(card);
  } else {
    cards.push({dataset: {...replacement}, remove() { cards = cards.filter(c => c !== this); }});
  }
  await refreshLoadedCardMeta("same_location_replacement");
  check(state.loadedVideos.includes(replacement) && !state.loadedVideos.some(v => v.id === "old-id"), "same_location_new_row_kept");
  check(grid.querySelectorAll(".card").length === 6, "same_location_new_dom_kept");
  reset();
  state.totalCount = state.rawCount = 6;
  pruneDeletedVideos([state.loadedVideos[0]]);
  check(state.loadedVideos.length === 5 && state.totalCount === 5 && state.rawCount === 5, "explicit_delete_counts_updated");
  reset();
  state.virtualEnabled = true;
  await refreshLoadedCardMeta("virtual");
  check(state.loadedVideos.length === 5 && scroller.scrollTop === 120, "virtual_missing_pruned");
  reset();
  state.hasMore = true;
  globalThis.fetchVideos = async () => ({videos: [{id: "new-id"}], count: 104, raw_count: 106, has_more: true});
  await loadMore("test");
  check(state.totalCount === 104 && state.rawCount === 106 && state.offset === 7, "append_counts_updated");
  globalThis.cardRefreshResult = {passed: checks.length, checks};
  return globalThis.cardRefreshResult;
}
runCardRefreshChecks().then(result => console.log(JSON.stringify(result))).catch(error => {
  globalThis.cardRefreshResult = {error: String(error.stack || error)};
  if (typeof process !== "undefined") process.exitCode = 1;
  console.error(error);
});
'''


def history_thumbnail_fixture_script(scenario):
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    functions = []
    for start, end in [
        ("function loadPlayProgressMap(", "function getPlayRow("),
        ("function historyHints(", "function beginPlaybackOperation("),
        ("function historyEntries(", "async function renderContinueBar("),
        ("async function renderContinueBar(", "    loadUiPrefs();"),
        ("function thumbUrlFor(", "function resetThumbQueue("),
        ("function watchThumbs(", "function queueThumb("),
        ("function applyThumbGaveUpDom(", "function applyCachedThumb("),
        ("function thumbLoadGiveUp(", "function thumbLoadFailed("),
        ("function sameLibPath(", "    // === Sidebar / tags fingerprint helpers"),
        ("function cardDurationLabel(", "    const LOADED_META_FIELDS"),
        ("function cardHtml(", "function getVirtualCols("),
        ("function formatResumeTime(", "function resumePlayback("),
        ("function escapeHtml(", "    // events"),
        ("function thumbHtml(", "function pickKeepId("),
        ("function historyDayLabel(", "async function loadHistory("),
        ("async function loadHistory(", "function closeHistoryModal("),
    ]:
        functions.append(start + html.split(start, 1)[1].split(end, 1)[0])
    return "\n".join(functions) + r'''
// Small in-memory DOM/storage fixture: no browser, server or real preferences.
const checks = [], logs = [], requests = [], thumbnailRequests = [], pending = [];
let storage = "", storageWrites = 0, nodes = {}, histKind = "resume", historyLoadVersion = 0;
const PLAY_PROG_KEY = "vg_play_progress";
const state = {root: "Z:\\", lib: ""};
const localStorage = {
  getItem(key) { if (key !== PLAY_PROG_KEY) throw new Error("unexpected storage read"); return storage; },
  setItem() { storageWrites++; throw new Error("history must not be written"); },
  removeItem() { storageWrites++; throw new Error("history must not be removed"); }
};
function check(condition, label) { if (!condition) throw new Error(label); checks.push(label); }
function decode(value) {
  return String(value).replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
}
function dataAttrs(markup) {
  const dataset = {};
  for (const m of markup.matchAll(/data-([\w-]+)="([^"]*)"/g)) {
    dataset[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = decode(m[2]);
  }
  return dataset;
}
class Chip {
  constructor(open, body) { this.open = open; this.body = body; this.dataset = dataAttrs(open); }
  get outerHTML() {
    const open = this.open.replace(/data-play-root="[^"]*"/, 'data-play-root="' + escapeAttr(this.dataset.playRoot || "") + '"');
    return open + this.body + "</button>";
  }
  querySelector(selector) {
    if (selector !== ".media-row-thumb") throw new Error("unexpected chip selector");
    const chip = this;
    return {set outerHTML(value) {
      chip.body = chip.body.replace(/<div class="media-row-thumb">[\s\S]*?<\/div>/, value);
    }};
  }
}
class Element {
  constructor() {
    this.dataset = {}; this.style = {}; this.chips = []; this.raw = "";
    this.classList = {toggle() {}}; this.attributes = {}; this.scrollTop = 0;
  }
  setAttribute(key, value) { this.attributes[key] = value; }
  set innerHTML(value) {
    this.raw = value;
    this.chips = [...value.matchAll(/(<button\b[^>]*>)([\s\S]*?)<\/button>/g)]
      .map(m => new Chip(m[1], m[2]));
  }
  get innerHTML() {
    let i = 0;
    return this.raw.replace(/<button\b[^>]*>[\s\S]*?<\/button>/g, () => this.chips[i++].outerHTML);
  }
  contains(chip) { return this.chips.includes(chip); }
  querySelectorAll(selector) {
    if (selector === "[data-play-id]") return this.chips;
    if (selector === "img[data-thumb-url]") {
      return [...this.innerHTML.matchAll(/<img\b([^>]*)>/g)].filter(m => /data-thumb-url=/.test(m[1]))
        .map(m => ({dataset: dataAttrs(m[1]), src: "", style: {}}));
    }
    throw new Error("unexpected DOM selector: " + selector);
  }
}
function $(id) { return nodes[id] || (nodes[id] = new Element()); }
function clientLog(event, fields, level) { logs.push({event, fields, level}); }
function workspaceEmptyHtml(title, detail) { return escapeHtml(title) + escapeHtml(detail); }
function response(data, ok = true) { return {ok, json: async () => data}; }
globalThis.fetch = (url, opts) => {
  if (url.startsWith("/thumb/")) {
    thumbnailRequests.push(url);
    return Promise.resolve({ok: true});
  }
  check(url === "/api/videos-by-ids", "only_by_ids_lookup");
  check(opts.method === "POST" && opts.headers["Content-Type"] === "application/json", "lookup_post_json");
  requests.push(JSON.parse(opts.body));
  return new Promise((resolve, reject) => pending.push({resolve, reject}));
};
function resolveNext(data, ok = true) { pending.shift().resolve(response(data, ok)); }
function row(root = "C:\\", extra = {}) {
  return {root, rel: "saved/movie.mp4", folder: "保存的目录", name: "保存的片名<&>", t: 25,
    duration: 100, updated: 1000, done: false, ...extra};
}
function reset(progress) {
  storage = JSON.stringify(progress); storageWrites = 0; nodes = {};
  logs.length = requests.length = thumbnailRequests.length = pending.length = 0;
}
// Invoke the extracted startup renderer before thumbnail constants initialize.
reset({"old-419-a": row(), "old-419-b": row("D:\\")});
const startup = renderContinueBar();
check($("continueBar").innerHTML.includes("保存的片名&lt;&amp;&gt;"), "startup_keeps_saved_name");
check(!$("continueBar").innerHTML.includes("data-thumb-url"), "startup_no_unverified_urls_or_tdz");
check(thumbnailRequests.length === 0, "startup_no_old_id_network");
const THUMB_PENDING_SRC = "data:image/svg+xml,pending";
const thumbObjectUrls = new Map(), thumbsLoaded = new Set(), thumbsAwaitCatalog = new Map();
function thumbPermanentlyFailed() { return false; }
function applyCachedThumb() { return false; }
function bindPageThumbObserver() {
  // Eager observation makes every emitted thumbnail URL a recorded request.
  return {observe(img) { fetch(img.dataset.thumbUrl); }};
}
function bindThumbObserver() { return bindPageThumbObserver(); }
function markThumbGaveUp() {}
function thumbIdFromUrl(url) { return decodeURIComponent((url.split("/thumb/")[1] || "").split("?")[0]); }
function rootFromThumbUrl(url) { return new URL(url, "http://fixture").searchParams.get("root") || ""; }
function noteThumbAwaitCatalog(url, reason, root) { thumbsAwaitCatalog.set(thumbIdFromUrl(url), {reason, root}); }
function logThumbDiag(event, fields, level) { clientLog(event, fields, level); }
function pumpThumbs() {}
function assertNoOldThumbnails(label) {
  check(thumbnailRequests.every(url => !url.includes("old-419")), label + "_no_old_thumbnail_network");
  check(storageWrites === 0, label + "_no_history_writes");
}
async function runHistoryThumbnailChecks(scenario) {
  resolveNext({ok: true, videos: [], missing: [{id: "old-419-a", confirmed: true}], catalog_stable: true});
  await startup;
  assertNoOldThumbnails("startup");
  if (scenario === "resolved") {
    const saved = {valid: row(), "old-419-a": row(), "old-419-b": row("D:\\"), rootless: row("")};
    reset(saved);
    const stored = storage;
    const render = renderContinueBar();
    const initial = $("continueBar").innerHTML;
    check(!initial.includes("data-thumb-url") && initial.includes('class="pct">25%'), "pending_text_and_progress");
    check(initial.includes('data-play-id="old-419-a"') && initial.includes("保存的目录"), "pending_history_clickable");
    check(JSON.stringify(requests[0].ids) === JSON.stringify(Object.keys(saved)), "all_saved_ids_looked_up");
    check(requests[0].hints.valid.root === "C:\\" && requests[0].hints.valid.rel === "saved/movie.mp4", "saved_root_rel_hints");
    check(requests[0].hints.rootless.root === "", "rootless_hint_not_active_root");
    const data = {ok: true, videos: [
      {id: "valid", root: "c:\\", thumb_id: "verified-cover", thumb_v: 9, has_thumb: true, name: "当前片名"},
      {id: "rootless", root: "E:\\", has_thumb: false}
    ], missing: [{id: "old-419-a", confirmed: false}, {id: "old-419-b", confirmed: true}], catalog_stable: true};
    resolveNext(data); await render;
    check(thumbnailRequests.length === 2 && thumbnailRequests.some(url => url.startsWith("/thumb/verified-cover?v=9")), "only_returned_thumbnails");
    check(thumbnailRequests.some(url => url.startsWith("/thumb/rootless?v=0")), "not_generated_still_requested");
    const chips = $("continueBar").chips;
    check(chips[0].dataset.playRoot === "C:\\" && chips[3].dataset.playRoot === "E:\\", "saved_or_returned_root_used");
    check(chips[1].body.includes("无图") && chips[2].body.includes("无图"), "unknown_ids_placeholder");
    check(chips.every(chip => chip.body.includes("保存的片名&lt;&amp;&gt;") && chip.body.includes("25%")), "lookup_preserves_saved_text");
    const modal = loadHistory("recent"); resolveNext(data); await modal;
    check($("historyBody").innerHTML.includes("当前片名") && $("historyBody").innerHTML.includes("暂未找到视频"), "modal_verified_and_missing_entries");
    check(storage === stored, "resolved_history_unchanged");
    assertNoOldThumbnails("resolved");
    reset({valid: row("C:/folder/media/")});
    const normalized = {ok: true, videos: [{id: "valid", root: "c:\\folder\\media", has_thumb: true}]};
    const normalizedBar = renderContinueBar(); resolveNext(normalized); await normalizedBar;
    check(thumbnailRequests.length === 1, "mixed_separator_continue_thumbnail");
    const normalizedHistory = loadHistory("recent"); resolveNext(normalized); await normalizedHistory;
    check($("historyBody").innerHTML.includes("待续播") && !$("historyBody").innerHTML.includes("暂未找到视频"), "mixed_separator_history_found");
    check(!sameLibPath("C:/folder/media", "D:\\folder\\media"), "different_drives_remain_distinct");
  } else if (scenario === "missing") {
    const cases = [
      {name: "unknown", data: {videos: []}},
      {name: "unconfirmed", data: {videos: [], missing: [{id: "old-419-a", confirmed: false}]}},
      {name: "stable_missing", data: {videos: [], missing: [{id: "old-419-a", root: "C:\\", confirmed: true}], catalog_stable: true}},
      {name: "offline", data: {videos: [], offline_roots: ["c:\\"]}},
      {name: "offline_cached", data: {videos: [{id: "old-419-a", root: "C:\\", has_thumb: true}], offline_roots: ["C:\\"]}},
      {name: "same_id_other_root", data: {videos: [{id: "old-419-a", root: "D:\\", has_thumb: true}]}},
      {name: "row_without_owner_root", data: {videos: [{id: "old-419-a", has_thumb: true}]}}
    ];
    for (const {name, data} of cases) {
      reset({"old-419-a": row()});
      const stored = storage;
      const render = renderContinueBar(); resolveNext(data); await render;
      check($("continueBar").innerHTML.includes("无图") && $("continueBar").innerHTML.includes("保存的片名"), name + "_continue_text_placeholder");
      check($("continueBar").chips[0].dataset.playRoot === "C:\\", name + "_saved_root_preserved");
      const modal = loadHistory("recent"); resolveNext(data); await modal;
      const markup = $("historyBody").innerHTML;
      check(markup.includes('data-hist-id="old-419-a"') && markup.includes("保存的片名") && markup.includes("0:25 / 1:40 · 25%"), name + "_modal_history_preserved");
      check(markup.includes("无图") && !markup.includes("data-thumb-url"), name + "_modal_placeholder");
      check(markup.includes(data.offline_roots ? "磁盘未连接" : "暂未找到视频"), name + "_modal_status");
      check(thumbnailRequests.length === 0 && stored === storage, name + "_no_network_or_pruning");
      assertNoOldThumbnails(name);
    }
    for (const mode of ["network", "http", "api"]) {
      reset({"old-419-a": row()});
      const render = renderContinueBar();
      if (mode === "network") pending.shift().reject(new Error("disconnected"));
      else resolveNext({ok: mode !== "api", msg: "lookup error", videos: [{id: "old-419-a", root: "C:\\"}]}, mode !== "http");
      await render;
      check($("continueBar").innerHTML.includes("保存的片名") && $("continueBar").innerHTML.includes("无图"), mode + "_saved_text_on_lookup_error");
      check(logs.some(log => log.event === "continue_bar_lookup_failed" && log.level === "WARN"), mode + "_new_errors_still_warn");
      check(thumbnailRequests.length === 0, mode + "_no_thumbnail_on_error");
      assertNoOldThumbnails(mode);
    }
  } else if (scenario === "stale") {
    reset({shared: row("C:\\")});
    const first = renderContinueBar(), firstPending = pending.shift();
    const oldChip = $("continueBar").chips[0];
    storage = JSON.stringify({shared: row("D:\\", {name: "换盘后记录"})});
    const second = renderContinueBar();
    resolveNext({videos: [{id: "shared", root: "D:\\", thumb_id: "new-root-cover"}]}); await second;
    const current = $("continueBar").innerHTML, requestCount = thumbnailRequests.length;
    firstPending.resolve(response({videos: [{id: "shared", root: "C:\\", thumb_id: "old-root-cover"}]})); await first;
    check($("continueBar").innerHTML === current && thumbnailRequests.length === requestCount, "stale_same_id_root_response_ignored");
    check(oldChip.body.includes("无图") && current.includes("换盘后记录"), "replaced_chip_untouched");
    check(!thumbnailRequests.some(url => url.includes("old-root-cover")), "no_old_root_network");
    const clearing = renderContinueBar(), clearingPending = pending.shift();
    storage = "{}"; await renderContinueBar();
    clearingPending.resolve(response({videos: [{id: "shared", root: "D:\\"}]})); await clearing;
    check($("continueBar").style.display === "none" && $("continueBar").innerHTML === "", "empty_render_invalidates_pending");
    reset({shared: row()});
    const replacing = renderContinueBar(), replacedBar = $("continueBar");
    nodes.continueBar = new Element();
    resolveNext({videos: [{id: "shared", root: "C:\\"}]}); await replacing;
    check(nodes.continueBar.innerHTML === "" && replacedBar.innerHTML.includes("无图") && thumbnailRequests.length === 0, "dom_identity_invalidates_pending");
    reset({shared: row()});
    const older = renderContinueBar(), olderPending = pending.shift();
    const newer = renderContinueBar(); resolveNext({videos: []}); await newer;
    olderPending.reject(new Error("obsolete lookup")); await older;
    check(logs.length === 0, "obsolete_error_does_not_replace_or_warn");
    const oldModal = loadHistory("recent"), oldModalPending = pending.shift();
    storage = JSON.stringify({shared: row("D:\\", {name: "新的历史"})});
    const newModal = loadHistory("recent"); resolveNext({videos: []}); await newModal;
    const modalMarkup = $("historyBody").innerHTML;
    oldModalPending.resolve(response({videos: [{id: "shared", root: "C:\\"}]})); await oldModal;
    check($("historyBody").innerHTML === modalMarkup && thumbnailRequests.length === 0, "modal_version_guard_retained");
    assertNoOldThumbnails("stale");
  } else if (scenario === "persisted") {
    const source = {id: "failed", root: "C:\\", size: 100, mtime: 10, has_thumb: false};
    const cases = [
      [{thumb_failed: true, thumb_failed_size: 100, thumb_failed_mtime: 10}, true, "current"],
      [{thumb_failed_reason: "decode", thumb_failed_size: 100, thumb_failed_mtime: 10}, true, "canonical_reason"],
      [{thumb_fail_reason: "decode", thumb_failed_size: 100, thumb_failed_mtime: 10}, true, "reason_alias"],
      [{thumb_failed: true}, true, "legacy"],
      [{thumb_failed: true, thumb_failed_size: 100, thumb_failed_mtime: 10.5}, true, "mtime_tolerance"],
      [{thumb_failed: true, thumb_failed_size: 99, thumb_failed_mtime: 10}, false, "size_changed"],
      [{thumb_failed: true, thumb_failed_size: 100, thumb_failed_mtime: 8}, false, "mtime_changed"],
      [{thumb_failed: true, thumb_failed_size: "invalid", thumb_failed_mtime: 10}, false, "invalid_stamp"],
      [{}, false, "not_generated"]
    ];
    for (const [extra, failed, name] of cases) {
      const item = {...source, ...extra};
      check(thumbnailFailureIsCurrent(item) === failed, name + "_source_match");
      check(thumbHtml(item.id, 0, false, item.root, item).includes("data-thumb-url") === !failed, name + "_row_markup");
      check(lazyThumbMarkup(item.id, 0, item.root, item).includes("data-thumb-url") === !failed, name + "_lazy_markup");
      check(cardHtml(item).includes("data-thumb-url") === !failed, name + "_card_markup");
      reset({failed: row()});
      const data = {videos: [item]};
      const render = renderContinueBar(); resolveNext(data); await render;
      const modal = loadHistory("recent"); resolveNext(data); await modal;
      check(thumbnailRequests.length === (failed ? 0 : 2), name + "_continue_modal_network");
      check($("continueBar").innerHTML.includes("保存的片名") && $("historyBody").innerHTML.includes("保存的片名"), name + "_history_text_retained");
      assertNoOldThumbnails(name);
    }
    check(!lazyThumbMarkup("", 0, "C:\\").includes("data-thumb-url"), "empty_lazy_id_placeholder");
  } else if (scenario === "logging") {
    reset({});
    for (const reason of ["persisted_failure", "persisted_failure", "new_decode_failure", "item_not_found", "catalog_transition", "unavailable"]) {
      const img = {dataset: {thumbUrl: "/thumb/log-id?v=0&root=C%3A%5C", thumbAttempt: "3"},
        closest() { return null; }, style: {}};
      thumbLoadGiveUp(img, reason);
      const log = logs[logs.length - 1];
      check(log.event === "thumb_give_up" && log.fields.reason === reason, reason + "_diagnostic_preserved");
      check(log.level === (reason === "persisted_failure" ? "INFO" : "WARN"), reason + "_expected_level");
      check(img.dataset.thumbReady === "1" && img.dataset.thumbExhausted === "1", reason + "_placeholder_state");
    }
    check(logs.filter(log => log.level === "WARN").length === 4, "only_persisted_failure_avoids_repeated_warn");
    check(thumbsAwaitCatalog.has("log-id"), "catalog_soft_failures_still_parked");
  } else {
    throw new Error("unknown fixture scenario: " + scenario);
  }
  return {scenario, passed: checks.length, checks};
}
''' + "\nrunHistoryThumbnailChecks(" + json.dumps(scenario) + r''').then(result => {
  console.log(JSON.stringify(result));
}).catch(error => { console.error(error.stack || error); process.exitCode = 1; });
'''


ROOT = Path(__file__).resolve().parents[1]


class UiControlLayoutTests(unittest.TestCase):
    def _run_history_thumbnail_fixture(self, scenario: str) -> None:
        result = subprocess.run(
            ["node"], input=history_thumbnail_fixture_script(scenario),
            text=True, encoding="utf-8", capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["scenario"], scenario)
        self.assertGreater(report["passed"], 10)

    def test_continue_bar_resolves_only_verified_thumbnail_ids(self) -> None:
        self._run_history_thumbnail_fixture("resolved")

    def test_history_missing_and_offline_preserve_saved_entries(self) -> None:
        self._run_history_thumbnail_fixture("missing")

    def test_continue_bar_discards_stale_thumbnail_lookups(self) -> None:
        self._run_history_thumbnail_fixture("stale")

    def test_persisted_thumbnail_failure_matches_source_not_has_thumb(self) -> None:
        self._run_history_thumbnail_fixture("persisted")

    def test_thumb_give_up_warns_only_for_unexpected_failures(self) -> None:
        self._run_history_thumbnail_fixture("logging")

    def test_card_refresh_missing_guards_and_append_counts(self) -> None:
        result = subprocess.run(
            ["node"], input=card_refresh_fixture_script(),
            text=True, encoding="utf-8", capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"passed":24', result.stdout)

    def test_sort_and_view_live_in_toolbar_dropdown(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        filters = html.split('<div class="filters" id="filters">', 1)[1].split(
            '<div class="scope-sticky" id="scopeSticky">', 1
        )[0]
        toolbar = html.split('<div class="scope-sticky" id="scopeSticky">', 1)[1].split(
            '<div class="status-bar" id="status">', 1
        )[0]

        self.assertNotIn('id="sortTags"', filters)
        self.assertNotIn('id="viewTags"', filters)
        self.assertIn('id="scopeFilterBtn"', toolbar)
        self.assertIn('id="scopeOptions"', toolbar)
        self.assertIn('id="sortTags"', toolbar)
        self.assertIn('id="viewTags"', toolbar)
        self.assertIn('id="scopeOptionsText"', toolbar)

    def test_thumbnail_images_use_non_blocking_retry_flow(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function thumbLoadFailed(img, reason, opts)", html)
        self.assertIn("function thumbLoadGiveUp(", html)
        self.assertIn("const thumbsGaveUp = new Set()", html)
        self.assertIn("function thumbPermanentlyFailed(", html)
        self.assertIn("markThumbGaveUp(url)", html)
        self.assertIn("const thumbObjectUrls = new Map()", html)
        self.assertIn("function applyCachedThumb(", html)
        self.assertIn("rememberThumbObjectUrl(url, objUrl)", html)
        self.assertIn("X-VG-Thumb-Status", html)
        self.assertIn("function requestThumbReload(img, url, opts)", html)
        self.assertIn("function resetThumbQueue(", html)
        self.assertIn("&defer=1", html)
        self.assertIn("THUMB_CONCURRENCY", html)
        self.assertIn("function watchThumbs(", html)
        self.assertIn("data-thumb-url", html)
        self.assertIn("refresh({ nav: false })", html)
        self.assertIn("resetThumbQueue()", html)
        self.assertIn("cancelListRequests()", html)
        self.assertIn("MAX_LOADED_VIDEOS", html)
        self.assertIn("AbortController", html)
        self.assertNotIn("dataset.retried", html)
        self.assertNotIn("const probe = new Image()", html)
        self.assertNotIn("const slowDelay = 15000", html)

    def test_multi_disk_all_videos_badge_uses_catalog_total(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("function catalogTotal(", html)
        self.assertIn("const allBadge = catalogTotal(total);", html)
        self.assertIn("renderChannels(state.categories || [], catalogTotal());", html)
        self.assertNotIn("renderChannels(state.categories || [], state.totalCount || 0)", html)
        self.assertNotIn(
            "const allBadge = (total != null && total > 0) ? total : (grand || 0);",
            html,
        )

    def test_feedback_and_probe_preferences_live_in_settings(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        header = html.split('<div class="header-actions">', 1)[1].split("</header>", 1)[0]
        settings = html.split('<div class="modal settings-modal" id="privacyModal">', 1)[1].split(
            '<div class="modal" id="cleanupModal">', 1
        )[0]

        self.assertIn('id="settingsBtn"', header)
        self.assertNotIn('id="feedbackBtn"', header)
        self.assertIn('id="probeVideoDuration"', settings)
        self.assertIn('id="probeVideoAudio"', settings)
        self.assertIn("反馈问题", settings)
        self.assertIn("overflow: auto", html.split(".settings-panel .modal-info", 1)[1].split("}", 1)[0])
        self.assertIn("settings-panel", settings)
        self.assertIn('id="privacySave"', settings.split('class="modal-actions"', 1)[1])

    def test_channel_switch_abort_is_not_treated_as_page_failure(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        abort_fn = html.split("function isAbortError(", 1)[1].split(
            "function cancelListRequests(", 1
        )[0]
        self.assertIn("channel-switch", abort_fn)
        self.assertIn("controller.signal.aborted", html)
        self.assertIn("if (isAbortError(reason)) return;", html)

    def test_user_actions_and_playback_failures_are_reported_with_operation_ids(self) -> None:
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('fetch("/api/client-log"', html)
        self.assertIn('"X-VG-Operation-ID"', html)
        self.assertIn('clientLog("ui_click"', html)
        self.assertIn('clientLog("page_load_requested"', html)
        self.assertIn('clientLog("player_error"', html)
        self.assertIn('clientLog("hls_error"', html)
        self.assertIn("function beginPlaybackOperation(", html)
        self.assertIn("CLIENT_LOG_CORE_EVENTS", html)
        self.assertIn("CLIENT_LOG_LOOP_EVENTS", html)
        self.assertIn("clientLogQueue", html)
        self.assertIn("events.length === 1 ? events[0] : { events }", html)
        self.assertIn("!state._fullLogging", html)


if __name__ == "__main__":
    unittest.main()
