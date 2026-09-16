#!/usr/bin/env python3
"""builder 自验（DEVELOP-P1-2 前端任务身份）：抽 `app/index.html` 真源码 + node 桩，
断言「轮询带 data_root+job_id、只渲染本页那次 job、别目录/别任务不串」。
不点真机、不起 8899、不请求线上服务。

做法：从 index.html 里按函数名**逐字抽真源码**（不手抄），拼上 DOM/fetch 桩跑
node；任一断言失败 node 退出码非 0，本脚本随之退出 1。

运行：python3 tests/selftest_p1_2_frontend.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "app", "index.html")

FUNCS = [
    "esc", "el", "dataRoot",
    "vocabApplyStopPoll", "vocabApplySetText", "renderVocabApplyProgress",
    "renderVocabApplyFinal", "vocabApplyStartTimer", "vocabApplyStatusQuery",
    "vocabApplyDropPoll", "vocabApplyNotePollFailure", "pollVocabApplyStatus",
    "vocabJobDataRoot", "refresh", "vocabApplyGiveUp", "vocabApplyFinish",
    "isAbsRoot",
    "resumeVocabApplyPoll", "sameDataRoot", "applyVocabCandidates",
    "reapplyPayload", "setCandidateControls", "candidateCheckboxes",
    "candidateGroupCheckboxes", "syncCandidateSelectAll", "loadVocabCandidates",
    # P1-3 新增/被新断言用到的函数（缺一个就会 ReferenceError → rc=1）
    "vocabApplyPct", "vocabApplyElapsedText", "vocabApplyStatsText",
    "vocabApplyLockText", "retryBody",
    # P1-3/CANDIDATE-UI2 P3-2 范围二选一（安全默认）
    "recScopeVal", "recScopeLabel", "syncRecScope", "recoverHint",
    # P1-3/RERUN-PROGRESS P3-4 主进度条无目标不画满
    "renderProgress", "stageStep",
    # P1-3 返工（P2-1/P2-2）：批量重试的文案与失败原因透传
    "retryRootHint", "retryRun", "retryAllFailed", "failedRuns", "isFailedRun",
    "filenameForRunId",
    # P1-4/FR-3/HD-2=A：脱敏摘要复制＋页面逐条本地证据（缺一个即 ReferenceError → rc=1）
    "redactPathField", "redactPathTail", "redactCopyField", "digestEntry",
    "digestDataRoot", "dropDiagCache", "loadDiag", "diagItemFor",
    "failDigestFields", "failDigestText", "evidenceLinesFor", "failEvidenceText",
    "renderDiagEvidence", "copyAllFailedReasons", "showFailEvidence",
    # P1-4 用到的既有函数：复制短句化（FR-17）与长文面板本体，抽真源码才验得动
    "copyText", "openLongPanel", "failPartsForRun", "filenameForRun", "shortId",
    # P1-5 词库列表展示层：分组判定（trim/大小写）与整块渲染（来源标签/空态/筛选计数）
    "vocabGroupFor", "renderVocabList",
    # P1-6/FR-15：data_root 摘要与隐藏键（sha256 紧凑实现＋localStorage 键分区）
    "sha256Hex", "hideDoneKey", "hideDoneGet", "hideDoneSet", "lsGet", "lsSet",
    # DEVELOP-P1-9：库里已有同名笔记 → 显示「已跳过（笔记已存在）」（不是排队中、不是失败）
    "statusCN", "cnOfState", "stClass",
]

# P1-4：模块常量/模块变量也照抄真源码，不手抄——常量改了测试跟着改，不会漂移
DECLS = ["FAIL_DIGEST_FIELDS", "REDACT_STOP_CHARS", "NO_EVIDENCE", "diagCache",
         "effectiveDataRoot", "lastLongText",
         # P1-5：词库分组标签表与当前词条表，照抄真源码
         "vocabGroupLabels", "loadedVocabEntries",
         # P1-6/FR-12：完成区分层状态（一行声明含四个变量，照抄整行）
         "completedExtra"]


def extract(src, name):
    """按函数名抓一段真源码（花括号配对，含嵌套）。"""
    marker = "function %s(" % name
    idx = src.find(marker)
    if idx < 0:
        raise AssertionError("index.html 找不到函数 %s（改名了？）" % name)
    start = idx
    brace = src.find("{", idx)
    depth = 0
    i = brace
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError("函数 %s 花括号不配对" % name)


def extract_decl(src, name):
    """按变量名抓 `var ...;` 声明**整行**真源码（常量/模块变量照抄，不手写复现）。"""
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("var ") and s.endswith(";") and ("%s=" % name) in s.replace(" ", ""):
            return s
    raise AssertionError("index.html 找不到变量声明 %s（改名了？）" % name)


DRIVER = r"""
// ------------------------------------------------------------------ DOM / fetch 桩
var REC = {text: {}, cls: {}, calls: [], urls: []};
// 真源码里这两个是模块常量，桩里按 index.html 同值复现
var VOCAB_APPLY_MAX_FAIL = 8, VOCAB_APPLY_MAX_MS = 10 * 60 * 1000;
function elStub(id){
  // P1-5：renderVocabList 会往 el("vocabList").innerHTML 写整块并在末尾重绑删除按钮，
  // 桩里给一个只收 HTML、没有删除按钮的容器（真按钮由 esc() 真源码生成，见 S11）
  if(id === "vocabList"){
    return {innerHTML: "", querySelectorAll: function(){ return []; }};
  }
  if(id === "candidateList"){
    return {
      innerHTML: "",
      _cb: [{checked: true, getAttribute: function(k){
        return k === "data-candidate-index" ? "0" : "high"; }, onchange: null}],
      querySelectorAll: function(sel){
        return sel === "[data-candidate-index]" ? this._cb : [];
      },
      querySelector: function(){ return null; }
    };
  }
  var o = {value: "", textContent: "", className: "", style: {}, disabled: false,
           checked: false, innerHTML: "", onclick: null, oninput: null};
  Object.defineProperty(o, "textContent", {
    get: function(){ return REC.text[id] || ""; },
    set: function(v){ REC.text[id] = String(v); }});
  Object.defineProperty(o, "className", {
    get: function(){ return REC.cls[id] || ""; },
    set: function(v){ REC.cls[id] = String(v); }});
  return o;
}
var ELS = {};
function el(id){ if(!ELS[id]) ELS[id] = elStub(id); return ELS[id]; }
var STORE = {};
var localStorage = {getItem: function(k){ return STORE[k] || null; },
                    setItem: function(k,v){ STORE[k] = v; },
                    removeItem: function(k){ delete STORE[k]; }};
var TIMERS = 0, TIMER_CB = null;
function setInterval(fn, ms){ TIMERS++; TIMER_CB = fn; return TIMERS; }
function clearInterval(){ TIMERS = 0; }
function confirm(){ return true; }
var FETCH_QUEUE = [], ROUTES = [];
// P2-1 反例用：按 url 子串把响应挂起（模拟「旧目录那条请求还在飞」），由用例手动放行
var HOLD = [];
function mkResp(r){ return {status: r.status,
                            json: function(){ return Promise.resolve(r.json); }}; }
function fetch(url, opts){
  var method = (opts && opts.method) || "GET";
  REC.urls.push(url);
  REC.calls.push({url: url, method: method, body: (opts && opts.body) || null});
  var held = null, hi;
  for(hi = 0; hi < HOLD.length; hi++){
    if(HOLD[hi].match && url.indexOf(HOLD[hi].match) >= 0
       && (!HOLD[hi].method || HOLD[hi].method === method)){ held = HOLD[hi]; break; }
  }
  if(held){
    var slot = {};
    held.queue.push(slot);
    return new Promise(function(res){ slot.release = function(r){ res(mkResp(r)); }; });
  }
  var best = null;   // 取“最长匹配”=最具体的路由，避免 /api/vocab 吃掉 /api/vocab/candidates
  for(var i = 0; i < ROUTES.length; i++){
    if(url.indexOf(ROUTES[i].match) >= 0
       && (!ROUTES[i].method || ROUTES[i].method === method)
       && (!best || ROUTES[i].match.length > best.match.length)){
      best = ROUTES[i];
    }
  }
  if(best){ return Promise.resolve(mkResp(best.res)); }
  var nxt = FETCH_QUEUE.shift() || {status: 200, json: {}};
  return Promise.resolve(mkResp(nxt));
}
function route(match, res, method){ ROUTES.push({match: match, res: res, method: method || null}); }
function tick(){ return new Promise(function(r){ setTimeout(r, 0); }); }
// refresh 用到的其它函数：桩里只做最小实现（本测试关心的是它的调用时序与 URL）
var lastLock = null, lastFetchAt = 0, refreshing = false;
var longInInput = 0;   // 真源码里的模块变量（renderProgress 读它）
function renderTape(){} function renderRuns(){} function renderLock(){}
function loadVocab(){} function loadPresets(){}
// P1-3：say 要能验（人话提示＝验收点），不再空实现
var SAYS = [];
function say(t){ SAYS.push(String(t == null ? "" : t)); }
// P1-3/P2-2：toast 也记一份（批量重试收尾会 toast）；真实现要 document.body，桩里不做
var TOASTS = [];
function toast(t){ TOASTS.push(String(t == null ? "" : t)); }
// failedRuns / filenameForRunId 读的模块变量（真源码里是 var cache={runs:[]} /
// var detailsByRun={}，桩里按同结构复现）
var cache = {runs: []};
var detailsByRun = {};
// P1-4：剪贴板桩——copyText 真源码走 navigator.clipboard.writeText，这里把写出去的
// 正文原样收下，供「摘要里到底有没有真实路径」的断言用
var CLIP = [];
var navigator = {clipboard: {writeText: function(t){ CLIP.push(String(t)); return Promise.resolve(); }}};
// P1-3/CANDIDATE-UI2 P3-2：recScope 单选桩（只实现被测函数用到的那几种选择器）
var RADIOS = [{value: "sel", checked: true}, {value: "all", checked: false}];
var document = {
  querySelector: function(sel){
    if(sel.indexOf('input[name="recScope"]:checked') >= 0){
      for(var i = 0; i < RADIOS.length; i++){ if(RADIOS[i].checked) return RADIOS[i]; }
      return null;
    }
    if(sel.indexOf('input[name="recScope"][value="sel"]') >= 0) return RADIOS[0];
    return null;
  },
  querySelectorAll: function(){ return []; }
};
// pollVocabApplyStatus 不返回 promise（fetch 链自带 .catch），必须显式冲刷微任务
async function poll(){ pollVocabApplyStatus(); await tick(); await tick(); }
async function flush(){ await tick(); await tick(); }
// 递归 .then 链（批量重试逐个 fetch→then→advance→step）要多轮冲刷；
// await 一次 tick 会排空微任务队列，这里多给几轮，避免 node 时序差异
async function settle(n){ for(var i = 0; i < n; i++){ await tick(); } }
var FAILS = [];
function ck(name, cond, extra){
  if(cond){ console.log("PASS " + name); }
  else { console.log("FAIL " + name + (extra === undefined ? "" : "  << " + JSON.stringify(extra)));
         FAILS.push(name); }
}
function reset(){
  ELS = {}; REC = {text: {}, cls: {}, calls: [], urls: []};
  vocabApplyJobId = null; vocabApplyTimer = null; vocabApplyRunning = false;
  vocabApplyFailStreak = 0; vocabApplyStartedAt = 0; vocabCandidatesRevision = null;
  effectiveDataRoot = "";   // 真源码里的模块变量（P2-新1）
  FETCH_QUEUE = []; TIMERS = 0; ROUTES = []; HOLD.length = 0;
  SAYS = []; RADIOS = [{value: "sel", checked: true}, {value: "all", checked: false}];
  TOASTS = []; cache = {runs: []}; detailsByRun = {};
  CLIP.length = 0; dropDiagCache();   // P1-4：剪贴板与诊断缓存每个用例清零，不跨用例串味
}

// ------------------------------------------------------------------ S1 本页 job 全程钉住
async function s1(){
  reset();
  el("inData").value = "/tmp/p12-fake-rootA";
  FETCH_QUEUE.push({status: 200, json: {ok: true, has_candidates: true, count: 1,
      candidates_revision: "REV-1",
      candidates: {high: [], medium: [], low: []}}});
  loadVocabCandidates();
  await flush();
  ck("S1 候选清单版本被记住", vocabCandidatesRevision === "REV-1",
     vocabCandidatesRevision);

  // 202 + 紧随其后的首次 status（前端 202 后会立刻 poll 一次）
  FETCH_QUEUE.push({status: 202, json: {ok: true, job_id: "job-A"}});
  FETCH_QUEUE.push({status: 200, json: {ok: true, job: {job_id: "job-A",
      state: "running", stage: "importing", rerun_old: true, total: 0, done: 0}}});
  applyVocabCandidates();
  await flush(); await flush();
  ck("S1 提交 202 后钉住本页 job_id", vocabApplyJobId === "job-A", vocabApplyJobId);
  var post = null, k;
  for(k = 0; k < REC.calls.length; k++){
    if(REC.calls[k].method === "POST"){ post = REC.calls[k]; }
  }
  ck("S1 POST 带 candidates_revision",
     !!post && post.body.indexOf('"candidates_revision":"REV-1"') >= 0,
     post && post.body);
  ck("S1 POST 带 data_root",
     !!post && post.body.indexOf('"data_root":"/tmp/p12-fake-rootA"') >= 0,
     post && post.body);
  var u = REC.urls[REC.urls.length - 1];
  ck("S1 轮询带 data_root", u.indexOf("data_root=%2Ftmp%2Fp12-fake-rootA") >= 0, u);
  ck("S1 轮询带 job_id", u.indexOf("job_id=job-A") >= 0, u);
  ck("S1 进行中照常渲染本页进度",
     (REC.text["candidateApplyText"] || "").indexOf("正在导入") >= 0,
     REC.text["candidateApplyText"]);
  ck("S1 进行中保持轮询表", TIMERS > 0);

  // 后端最近任务换成别的 job（另一标签页）→ 不渲染、停表、给人话
  FETCH_QUEUE.push({status: 200, json: {ok: true, job: {job_id: "job-B",
      state: "running", stage: "rerunning", total: 9, done: 4,
      message: "B页的任务", current_filename: "B视频.mp4"}}});
  await poll();
  var txt = REC.text["candidateApplyText"] || "";
  ck("S1 别页 job 不渲染（无 B 的进度）",
     txt.indexOf("B页") < 0 && txt.indexOf("4/9") < 0, txt);
  ck("S1 别页 job 给人话并停表",
     txt.indexOf("不是本页发起") >= 0 && TIMERS === 0, txt);

  // 服务端明确拒绝（跨目录 409）→ 不渲染、给人话、停表
  reset();
  el("inData").value = "/tmp/p12-fake-rootA";
  vocabApplyJobId = "job-A"; vocabApplyRunning = true;
  FETCH_QUEUE.push({status: 409, json: {ok: false, job: null,
      error: "最近一次任务属于另一个数据目录（零渲染），请核对数据目录后重查"}});
  await poll();
  var txt2 = REC.text["candidateApplyText"] || "";
  ck("S1 跨目录 409 不渲染他人的 job",
     txt2.indexOf("另一个数据目录") >= 0 && txt2.indexOf("正在导入") < 0, txt2);
  ck("S1 跨目录 409 后按钮解禁", el("btnCandidateApply").disabled === false);
}

// ------------------------------------------------------------------ S2 页面重载续看只认本目录
async function s2(){
  reset();
  el("inData").value = "/tmp/p12-fake-rootA";
  FETCH_QUEUE.push({status: 200, json: {ok: true, job: {job_id: "job-X",
      state: "running", data_root: "/tmp/p12-other-root/data", total: 3, done: 1}}});
  resumeVocabApplyPoll();
  await flush(); await flush();
  ck("S2 别目录的进行中任务不接管", TIMERS === 0 && vocabApplyJobId === null,
     [TIMERS, vocabApplyJobId]);

  reset();
  el("inData").value = "/tmp/p12-fake-rootA";
  FETCH_QUEUE.push({status: 200, json: {ok: true, job: {job_id: "job-Y",
      state: "running", data_root: "/tmp/p12-fake-rootA", total: 3, done: 1}}});
  resumeVocabApplyPoll();
  await flush(); await flush();
  ck("S2 本目录进行中任务接管并钉 job_id",
     TIMERS > 0 && vocabApplyJobId === "job-Y", [TIMERS, vocabApplyJobId]);

  reset();
  el("inData").value = "";
  FETCH_QUEUE.push({status: 200, json: {ok: true, job: {job_id: "job-Z",
      state: "running", data_root: "/tmp/p12-fake-rootA"}}});
  resumeVocabApplyPoll();
  await flush(); await flush();
  ck("S2 无 data_root 时不接管（无法判隔离宁可不画）",
     TIMERS === 0 && vocabApplyJobId === null, [TIMERS, vocabApplyJobId]);
}

// ------------------------------------------------------------------ S3 提交前置门
async function s3(){
  reset();
  el("inData").value = "/tmp/p12-fake-rootA";
  vocabCandidatesRevision = null;
  applyVocabCandidates();
  await flush();
  ck("S3 无候选版本时不提交（提示先刷新）",
     REC.calls.length === 0
     && (REC.text["vCandidate"] || "").indexOf("版本") >= 0,
     [REC.calls.length, REC.text["vCandidate"]]);
}

// ------------------------------------------------------------------ S4 服务端抖动不静默冻结
async function s4(){
  reset();
  el("inData").value = "/tmp/p12-fake-rootA";
  vocabApplyJobId = "job-A"; vocabApplyRunning = true;
  vocabApplyStartTimer();
  var timers_before = TIMERS;
  FETCH_QUEUE.push({status: 500, json: {ok: false, error: "服务开小差：x，稍后重试"}});
  await poll();
  ck("S4 5xx 不当作“非本页任务”停表",
     TIMERS > 0 && timers_before > 0,
     [TIMERS, timers_before]);
  ck("S4 5xx 走轮询容错口径并给人话",
     (REC.text["candidateApplyText"] || "").indexOf("进度读取失败，正在重试") >= 0,
     REC.text["candidateApplyText"]);
  ck("S4 5xx 不清掉本页 job 身份", vocabApplyJobId === "job-A", vocabApplyJobId);
}

// ------------------------------------------------------------------ S5 候选清单缺失：哨兵版本不当锁用
async function s5(){
  reset();
  el("inData").value = "/tmp/p12-fake-rootA";
  FETCH_QUEUE.push({status: 200, json: {ok: true, has_candidates: false, count: 0,
      candidates_revision: "absent", candidates: {high: [], medium: [], low: []}}});
  loadVocabCandidates();
  await flush();
  ck("S5 哨兵版本 absent 不当可用版本（前端置空）",
     vocabCandidatesRevision === null, vocabCandidatesRevision);
  var before = REC.calls.length;
  applyVocabCandidates();
  await flush();
  ck("S5 无可用版本时不提交（前置拒绝，不建空 job）",
     REC.calls.length === before
     && (REC.text["vCandidate"] || "").indexOf("版本") >= 0,
     [REC.calls.length - before, REC.text["vCandidate"]]);

  reset();
  el("inData").value = "/tmp/p12-fake-rootA";
  FETCH_QUEUE.push({status: 200, json: {ok: true, has_candidates: true, count: 1,
      candidates_revision: "REV-9", candidates: {high: [], medium: [], low: []}}});
  loadVocabCandidates();
  await flush();
  ck("S5 正常版本仍被记住（未误伤）", vocabCandidatesRevision === "REV-9",
     vocabCandidatesRevision);
}

// ------------------------------------------ S6 P2-新1：数据目录框留空（用默认外置测试目录）
var DEFAULT_ROOT = "/tmp/p12-fake-default";

function lastCall(match, method){
  for(var i = REC.calls.length - 1; i >= 0; i--){
    if(REC.calls[i].url.indexOf(match) >= 0
       && (!method || REC.calls[i].method === method)) return REC.calls[i];
  }
  return null;
}
function callsSince(n, match){
  var out = [];
  for(var i = n; i < REC.calls.length; i++){
    if(REC.calls[i].url.indexOf(match) >= 0) out.push(REC.calls[i]);
  }
  return out;
}

async function s6(){
  // 6a 框留空 + 已从同一真源学到生效目录 → 提交带显式 data_root，且轮询同口径
  reset();
  el("inData").value = "";
  route("/api/vocab/candidates/apply/status", {status: 200, json: {ok: true, job: {
      job_id: "job-D", state: "running", stage: "importing", rerun_old: false,
      data_root: DEFAULT_ROOT, total: 0, done: 0}}});
  route("/api/vocab/candidates", {status: 200, json: {ok: true, has_candidates: true,
      count: 1, candidates_revision: "REV-1", data_root: DEFAULT_ROOT,
      candidates: {high: [], medium: [], low: []}}});
  loadVocabCandidates();
  await flush();
  ck("S6 框留空时从候选清单回包学到生效目录",
     effectiveDataRoot === DEFAULT_ROOT, effectiveDataRoot);
  ck("S6 vocabJobDataRoot 在框留空时回该生效目录",
     vocabJobDataRoot() === DEFAULT_ROOT, vocabJobDataRoot());

  route("/api/vocab/candidates/apply", {status: 202, json: {ok: true, job_id: "job-D"}},
        "POST");
  applyVocabCandidates();
  await flush(); await flush();
  var post = lastCall("/api/vocab/candidates/apply", "POST");
  ck("S6① 框留空时 apply 仍带显式 data_root",
     !!post && post.body.indexOf('"data_root":"' + DEFAULT_ROOT + '"') >= 0,
     post && post.body);
  var pollUrl = (lastCall("/api/vocab/candidates/apply/status") || {}).url || "";
  ck("S6① 轮询带同一 data_root＋job_id",
     pollUrl.indexOf("data_root=" + encodeURIComponent(DEFAULT_ROOT)) >= 0
     && pollUrl.indexOf("job_id=job-D") >= 0, pollUrl);
  ck("S6① 进度照常渲染（未静默）",
     (REC.text["candidateApplyText"] || "").indexOf("正在导入") >= 0 && TIMERS > 0,
     [REC.text["candidateApplyText"], TIMERS]);

  // 6b 绝无「提交成功但 job:null 静默停表」：即便后端回 job:null 也必须给人话
  reset();
  el("inData").value = "";
  effectiveDataRoot = DEFAULT_ROOT;
  vocabApplyJobId = "job-D"; vocabApplyRunning = true;
  route("/api/vocab/candidates/apply/status", {status: 200, json: {ok: true, job: null}});
  await poll();
  ck("S6② job:null 不再静默停表（给人话、按钮复原）",
     TIMERS === 0 && el("btnCandidateApply").disabled === false
     && (REC.text["candidateApplyText"] || "").indexOf("查不到") >= 0,
     [TIMERS, REC.text["candidateApplyText"]]);

  // 6c 解析不到生效目录 → 人话阻止，绝不提交（不产生“后台跑着但看不到”的任务）
  reset();
  el("inData").value = "";
  vocabCandidatesRevision = "REV-1";   // 版本在手但目录未知
  var before = REC.calls.length;
  applyVocabCandidates();
  await flush();
  ck("S6① 目录未知时不提交（无人话则视为静默）",
     REC.calls.length === before
     && (REC.text["vCandidate"] || "").indexOf("数据目录") >= 0,
     [REC.calls.length - before, REC.text["vCandidate"]]);

  // 6d 续看同口径：框留空但生效目录已知 → 接管；目录未知 → 不接管也不发请求
  reset();
  el("inData").value = "";
  effectiveDataRoot = DEFAULT_ROOT;
  route("/api/vocab/candidates/apply/status", {status: 200, json: {ok: true, job: {
      job_id: "job-D2", state: "running", data_root: DEFAULT_ROOT, total: 3, done: 1}}});
  resumeVocabApplyPoll();
  await flush(); await flush();
  var resUrl = (lastCall("/api/vocab/candidates/apply/status") || {}).url || "";
  ck("S6③ 框留空续看：带生效目录并接管",
     TIMERS > 0 && vocabApplyJobId === "job-D2"
     && resUrl.indexOf("data_root=" + encodeURIComponent(DEFAULT_ROOT)) >= 0,
     [TIMERS, vocabApplyJobId, resUrl]);

  reset();
  el("inData").value = "";
  before = REC.calls.length;
  resumeVocabApplyPoll();
  await flush();
  ck("S6③ 目录未知时续看不发请求（不留半状态）",
     REC.calls.length === before && TIMERS === 0, REC.calls.length - before);

  // 6e 框留空 + /api/start 兜底真源（候选清单还没回包时的续看）
  reset();
  el("inData").value = "";
  route("/api/status", {status: 200, json: {ok: true, runs: []}});
  route("/api/start", {status: 200, json: {ok: true, running: false,
      data_root: null, default_data_root: DEFAULT_ROOT}});
  route("/api/vocab", {status: 200, json: {ok: true, vocab: []}});
  route("/api/vocab/presets", {status: 200, json: {ok: true, presets: []}});
  route("/api/vocab/candidates", {status: 200, json: {ok: true, has_candidates: false,
      count: 0, candidates_revision: "absent", data_root: DEFAULT_ROOT,
      candidates: {high: [], medium: [], low: []}}});
  route("/api/vocab/candidates/apply/status", {status: 200, json: {ok: true, job: {
      job_id: "job-E", state: "running", data_root: DEFAULT_ROOT, total: 2, done: 0}}});
  refresh(false);
  await flush(); await flush(); await flush();
  ck("S6③ refresh 后继看拿到 /api/start 的默认目录并接管",
     effectiveDataRoot === DEFAULT_ROOT && TIMERS > 0
     && vocabApplyJobId === "job-E", [effectiveDataRoot, TIMERS, vocabApplyJobId]);
  var rUrls = callsSince(0, "/api/vocab/candidates/apply/status");
  ck("S6③ 该次续看请求带显式 data_root",
     rUrls.length >= 1
     && rUrls[rUrls.length - 1].url.indexOf("data_root=" + encodeURIComponent(DEFAULT_ROOT)) >= 0,
     rUrls.map(function(c){ return c.url; }));

  // 6f 框有值时不受生效目录影响（真源优先级：框 > 学到值）
  reset();
  el("inData").value = "/tmp/p12-fake-typed";
  effectiveDataRoot = DEFAULT_ROOT;
  ck("S6 框有值时以框为准（不误用生效目录）",
     vocabJobDataRoot() === "/tmp/p12-fake-typed", vocabJobDataRoot());
}

// ------------------------------- S7 P2-三1：提交遇 409 running 的接管路径不许静默停滞
async function s7(){
  var ROOT_A = "/tmp/p12-fake-rootA";
  // 场景 C：正在跑的任务属于另一个 data_root（status 回 409）
  reset();
  el("inData").value = ROOT_A;
  effectiveDataRoot = ROOT_A;
  vocabCandidatesRevision = "REV-1";
  route("/api/vocab/candidates/apply", {status: 409, json: {ok: false, running: true,
      job_id: "job-other", error: "已有一次错词重跑在进行中，请等它跑完再试"}}, "POST");
  route("/api/vocab/candidates/apply/status", {status: 409, json: {ok: false, job: null,
      error: "最近一次任务属于另一个数据目录（零渲染），请核对数据目录后重查"}});
  applyVocabCandidates();
  await flush(); await flush(); await flush();
  var txtC = REC.text["candidateApplyText"] || "";
  ck("S7(C) 跨目录接管失败：有人话（不留“正在导入…”假象）",
     txtC.indexOf("另一个数据目录") >= 0, txtC);
  ck("S7(C) 跨目录接管失败：停表且按钮复原（不卡死）",
     TIMERS === 0 && el("btnCandidateApply").disabled === false,
     [TIMERS, el("btnCandidateApply").disabled]);

  // 场景 D：409 之后任务已跑完（status 回终态）→ 直接落终态，不是静默丢
  reset();
  el("inData").value = ROOT_A;
  effectiveDataRoot = ROOT_A;
  vocabCandidatesRevision = "REV-1";
  route("/api/vocab/candidates/apply", {status: 409, json: {ok: false, running: true,
      error: "已有一次错词重跑在进行中，请等它跑完再试"}}, "POST");
  route("/api/vocab/candidates", {status: 200, json: {ok: true, has_candidates: true,
      count: 1, candidates_revision: "REV-1", data_root: ROOT_A,
      candidates: {high: [], medium: [], low: []}}});
  route("/api/vocab/candidates/apply/status", {status: 200, json: {ok: true, job: {
      job_id: "job-F", state: "done", data_root: ROOT_A, imported: 2,
      rerun_old: false, summary: {success: 0, skipped: 0, failed: 0},
      message: "已导入2条；未重跑老稿，老稿未动"}}});
  applyVocabCandidates();
  await flush(); await flush(); await flush();
  var txtD = REC.text["candidateApplyText"] || "";
  ck("S7(D) 接管时任务已跑完：直接落终态并显示结果",
     txtD.indexOf("已导入2条") >= 0, txtD);
  ck("S7(D) 终态后按钮复原、无计时器",
     TIMERS === 0 && el("btnCandidateApply").disabled === false,
     [TIMERS, el("btnCandidateApply").disabled]);

  // 场景 G：409 后确实接管成功（同目录在跑）→ 落人话且进入进度
  reset();
  el("inData").value = ROOT_A;
  effectiveDataRoot = ROOT_A;
  vocabCandidatesRevision = "REV-1";
  route("/api/vocab/candidates/apply", {status: 409, json: {ok: false, running: true,
      error: "已有一次错词重跑在进行中，请等它跑完再试"}}, "POST");
  route("/api/vocab/candidates/apply/status", {status: 200, json: {ok: true, job: {
      job_id: "job-G", state: "running", data_root: ROOT_A, stage: "importing",
      rerun_old: false, total: 0, done: 0}}});
  applyVocabCandidates();
  await flush(); await flush(); await flush();
  ck("S7(G) 接管成功：钉住对方的 job_id 并继续显示进度",
     vocabApplyJobId === "job-G" && TIMERS > 0
     && (REC.text["candidateApplyText"] || "").indexOf("正在导入") >= 0,
     [vocabApplyJobId, TIMERS, REC.text["candidateApplyText"]]);

  // 场景 E：P3-三1 相对路径 -> 本地拦（不发请求）
  reset();
  el("inData").value = "relative/x";
  effectiveDataRoot = "";
  vocabCandidatesRevision = "REV-1";
  var n0 = REC.calls.length;
  applyVocabCandidates();
  await flush();
  ck("S7(E) 相对数据目录：本地人话拦住、不发请求",
     REC.calls.length === n0
     && (REC.text["vCandidate"] || "").indexOf("绝对路径") >= 0,
     [REC.calls.length - n0, REC.text["vCandidate"]]);

  // 场景 F：P3-三1 续看遇相对目录 -> 人话＋复原按钮（不静默）
  reset();
  el("inData").value = "relative/x";
  effectiveDataRoot = "";
  vocabApplyRunning = true;
  el("btnCandidateApply").disabled = true;
  n0 = REC.calls.length;
  resumeVocabApplyPoll();
  await flush();
  ck("S7(F) 续看遇相对目录：不发请求、给人话、按钮复原",
     REC.calls.length === n0 && TIMERS === 0
     && el("btnCandidateApply").disabled === false
     && (REC.text["candidateApplyText"] || "").indexOf("绝对路径") >= 0,
     [REC.calls.length - n0, REC.text["candidateApplyText"],
      el("btnCandidateApply").disabled]);
}

// ---------------- S8 P1-3：无目标不画满 / 运行中参数锁定 / 双空安全默认 / retry 带目录
async function s8(){
  // 8a 无目标终态（零目标）：进度条不画满、不画绿满，文案点明零目标
  reset();
  renderVocabApplyFinal({state: "done", total: 0, done: 0, imported: 2,
    rerun_old: false,
    message: "已导入2条；本次没有可重跑的已完成任务（零目标，未重跑任何稿件）",
    summary: {success: 0, skipped: 0, failed: 0, total: 0, needs_human: 0, interrupted: 0},
    stats: {total: 0, done: 0, success: 0, failed: 0, skipped: 0, needs_human: 0,
            interrupted: 0, counted: 0, balanced: true}});
  ck("S8a 零目标终态不画满（宽度 0%，旧形态是 100%）",
     el("candidateApplyFill").style.width === "0%",
     el("candidateApplyFill").style.width);
  ck("S8a 零目标终态文案点明「零目标／不画满」",
     (REC.text["candidateApplyText"] || "").indexOf("零目标") >= 0,
     REC.text["candidateApplyText"]);
  ck("S8a 汇总按后端同一口径展示（重跑目标 0 篇）",
     (REC.text["candidateApplyText"] || "").indexOf("重跑目标 0 篇") >= 0,
     REC.text["candidateApplyText"]);

  // 8b 对照：真有目标且跑完 → 才允许 100%（证明 8a 不是「永远 0%」的恒真断言）
  reset();
  renderVocabApplyFinal({state: "done", total: 4, done: 4, imported: 1,
    rerun_old: true, message: "已导入1条；重跑成功3篇，跳过1篇",
    summary: {success: 3, skipped: 1, failed: 0, total: 4, needs_human: 0, interrupted: 0},
    stats: {total: 4, done: 4, success: 3, failed: 0, skipped: 1, needs_human: 0,
            interrupted: 0, counted: 4, balanced: true}});
  ck("S8b 有目标跑完才 100%（对照）",
     el("candidateApplyFill").style.width === "100%",
     el("candidateApplyFill").style.width);
  ck("S8b 失败统计逐条可复算（4=3+1+0+0+0）",
     (REC.text["candidateApplyText"] || "").indexOf("可复算：4=3+1+0+0+0") >= 0,
     REC.text["candidateApplyText"]);

  // 8c 运行中：真实进度＋已用时＋可离开＋本次锁定的参数（页面＝实际执行）
  reset();
  renderVocabApplyProgress({state: "running", stage: "rerunning", rerun_old: true,
    total: 4, done: 1, elapsed_seconds: 65, indices_count: 3,
    current_filename: "a.mp4"});
  var t8c = REC.text["candidateApplyText"] || "";
  ck("S8c 运行中按真实进度画 25%",
     el("candidateApplyFill").style.width === "25%",
     el("candidateApplyFill").style.width);
  ck("S8c 显示已用时", t8c.indexOf("已用时 65s") >= 0, t8c);
  ck("S8c 提示可离开且回来仍能看到进度", t8c.indexOf("可离开本页面") >= 0, t8c);
  ck("S8c 显示本次锁定的参数（rerun_old＋目标候选数）",
     t8c.indexOf("本次锁定") >= 0 && t8c.indexOf("目标候选 3 条") >= 0, t8c);

  // 8d 运行中锁定：控件禁用＋写原因；别的批量链跑完（显式 lock=false）也解不开本页的锁
  reset();
  vocabApplyRunning = true;
  setCandidateControls(true, false);
  ck("S8d 运行中策略/范围/批量入口一律禁用",
     el("candidateRerunOld").disabled === true
     && el("candidateOnlyNew").disabled === true
     && el("candidateSelectAll").disabled === true
     && el("recScopeSel").disabled === true && el("recScopeAll").disabled === true
     && el("btnRecTranscribe").disabled === true && el("btnRecReuse").disabled === true
     && el("btnRecPublish").disabled === true,
     [el("candidateRerunOld").disabled, el("recScopeSel").disabled]);
  ck("S8d 运行中就地说清为什么不能改",
     (REC.text["batchLockHint"] || "").indexOf("已锁定") >= 0,
     REC.text["batchLockHint"]);
  ck("S8d lock=false 也解不开运行中的锁（不白等一次后端 409）",
     el("btnCandidateApply").disabled === true);

  // 8e 终态解除锁定（跑完才能改下一次的参数）
  reset();
  vocabApplyRunning = false;
  setCandidateControls(true, false);
  ck("S8e 跑完后控件放开、锁定提示清空",
     el("candidateRerunOld").disabled === false && el("recScopeAll").disabled === false
     && el("btnRecTranscribe").disabled === false
     && (REC.text["batchLockHint"] || "") === "",
     [el("candidateRerunOld").disabled, REC.text["batchLockHint"]]);

  // 8f 策略二选一（重跑老稿／只入库）双空 → 提交前回到安全一侧（只入库）
  reset();
  el("inData").value = "/tmp/p13-fake-root";
  vocabCandidatesRevision = "REV-1";
  el("candidateRerunOld").checked = false;
  el("candidateOnlyNew").checked = false;
  route("/api/vocab/candidates/apply",
        {status: 202, json: {ok: true, job_id: "job-P"}}, "POST");
  route("/api/vocab/candidates/apply/status", {status: 200, json: {ok: true, job: {
      job_id: "job-P", state: "running", stage: "importing", rerun_old: false,
      data_root: "/tmp/p13-fake-root", total: 0, done: 0}}});
  applyVocabCandidates();
  await flush(); await flush();
  var post8f = lastCall("/api/vocab/candidates/apply", "POST");
  ck("S8f 双空不提交空语义：明确回到安全一侧（只入库）",
     el("candidateOnlyNew").checked === true && el("candidateRerunOld").checked === false,
     [el("candidateOnlyNew").checked, el("candidateRerunOld").checked]);
  ck("S8f 页面显示与实际执行一致（提交体 rerun_old:false）",
     !!post8f && post8f.body.indexOf('"rerun_old":false') >= 0, post8f && post8f.body);
  ck("S8f 双空给人话原因（不静默换语义）",
     SAYS.join(" | ").indexOf("不能都不选") >= 0, SAYS);

  // 8g 409 锁定回显：把「本次锁住的参数」解析成人话（页面参数＝后台那次）
  ck("S8g 409 锁定回显解析（rerun_old＋目标候选数）",
     vocabApplyLockText({locked_params: {rerun_old: true, indices: [0, 1]}})
       .indexOf("重跑老稿") >= 0
     && vocabApplyLockText({locked_params: {rerun_old: true, indices: [0, 1]}})
       .indexOf("目标候选 2 条") >= 0,
     vocabApplyLockText({locked_params: {rerun_old: true, indices: [0, 1]}}));
  ck("S8g 没有锁定信息时不编造（回空串）", vocabApplyLockText({}) === "",
     vocabApplyLockText({}));

  // 8h 范围二选一（recScope）双空 → 回到安全一侧并写人话
  reset();
  RADIOS[0].checked = false; RADIOS[1].checked = false;
  var v8h = recScopeVal();
  ck("S8h 范围双空回到安全一侧（当前所选任务）",
     v8h === "sel" && RADIOS[0].checked === true, [v8h, RADIOS[0].checked]);
  ck("S8h 范围双空给人话原因",
     (REC.text["recoverHint"] || "").indexOf("不能为空") >= 0,
     REC.text["recoverHint"]);
  ck("S8h 范围选定后不被改写（不误伤已有选择）",
     (function(){ RADIOS[1].checked = true; RADIOS[0].checked = false;
                  return recScopeVal() === "all"; })());

  // 8i P2-7：/api/retry 必须带本页数据目录（框优先，其次生效目录，都没有才不带）
  reset();
  el("inData").value = "/tmp/p12-fake-rootA";
  var b8i = retryBody("run-1");
  ck("S8i 重试请求带本页数据目录（旧形态不带）",
     b8i.data_root === "/tmp/p12-fake-rootA" && b8i.run_id === "run-1", b8i);
  reset();
  el("inData").value = "";
  effectiveDataRoot = DEFAULT_ROOT;
  ck("S8i 框留空时用生效目录（与 apply 同口径）",
     retryBody("run-1").data_root === DEFAULT_ROOT, retryBody("run-1"));
  reset();
  el("inData").value = "";
  effectiveDataRoot = "";
  ck("S8i 两处都没有时不发 data_root（后端按监听目录走旧行为）",
     !("data_root" in retryBody("run-1")), retryBody("run-1"));

  // 8j 主进度条：无目标（total=0）不画满——哪怕「正在跑这一个」
  reset();
  renderProgress({current: {run_id: "r1", filename: "a.mp4", stage: "听写",
                            stage_started_at: new Date().toISOString()},
                  queue: {pending: 0, done: 0, failed: 0, total: 0}});
  ck("S8j 无目标三段全 0%（旧的灰条会满格＝画满）",
     el("progFillOk").style.width === "0%" && el("progFillFail").style.width === "0%"
     && el("progFillPending").style.width === "0%",
     [el("progFillOk").style.width, el("progFillFail").style.width,
      el("progFillPending").style.width]);
  ck("S8j 无目标但正在跑：文案仍说明在跑什么（不静默）",
     (REC.text["progText"] || "").indexOf("正在") >= 0, REC.text["progText"]);
  // 8j2 对照：真有目标才按实际推进（证明 8j 不是恒 0%）
  reset();
  renderProgress({current: null, queue: {pending: 0, done: 3, failed: 1, total: 4}});
  ck("S8j2 有目标按实际推进（成功 75%／失败 25%）",
     el("progFillOk").style.width === "75%" && el("progFillFail").style.width === "25%",
     [el("progFillOk").style.width, el("progFillFail").style.width]);
}

// ------------- S9 P1-3 返工：批量重试的「能不能离开」文案＋失败原因不再被静默吞掉
// P2-1 对应 9a/9b（反向证伪：把文案改回「可离开本页面，稍后回来仍能看到结果」→ rc=1）
// P2-2 对应 9c/9d/9e/9f（反向证伪：noteBad 退回只 badN++、retryBody 去掉本地门 → rc=1）
var S9_ROOT_A = "/tmp/p12-fake-rootA", S9_ROOT_B = "/tmp/p12-fake-rootB";
function retryCalls(){
  var out = [];
  for(var i = 0; i < REC.calls.length; i++){
    if(REC.calls[i].method === "POST"
       && REC.calls[i].url.indexOf("/api/retry") === 0) out.push(REC.calls[i]);
  }
  return out;
}
function twoFailed(){
  cache.runs = [{run_id: "run-1", status: "FAILED", source_filename: "a.mp4"},
                {run_id: "run-2", status: "FAILED", source_filename: "b.mp4"}];
}
async function s9(){
  // 9a 批量重试是**页面内循环**：不许承诺「可离开/后台继续」，必须说「保持本页打开」
  reset();
  el("inData").value = S9_ROOT_A;
  twoFailed();
  route("/api/retry", {status: 202, json: {ok: true}}, "POST");
  retryAllFailed();
  var first = SAYS[SAYS.length - 1] || "";
  ck("9a 首条文案不承诺可离开（旧形态：可离开本页面，稍后回来仍能看到结果）",
     first.indexOf("可离开") < 0, first);
  ck("9a 首条文案明说需保持本页打开（与真实行为逐字对得上）",
     first.indexOf("需保持本页打开") >= 0 && first.indexOf("不会继续重试") >= 0, first);
  await settle(6);
  var all9a = SAYS.join(" | ");
  ck("9a 逐条进度文案同样不提可离开",
     all9a.indexOf("可离开") < 0, SAYS);
  ck("9a 逐条进度文案说清离开的后果（剩余不再排队）",
     all9a.indexOf("离开则剩余任务不会再排队") >= 0, SAYS);
  ck("9a 真发出两条重试并报 2/2（文案没换掉实际行为）",
     retryCalls().length === 2 && all9a.indexOf("已排队 2/2") >= 0,
     [retryCalls().length, SAYS]);

  // 9b 共用锁定提示（错词重跑/重新成稿/批量重试三链同用）不得替用户承诺能离开
  reset();
  vocabApplyRunning = true;
  setCandidateControls(true, false);
  var hint9b = REC.text["batchLockHint"] || "";
  ck("9b 共用锁定提示仍写明锁定范围（三链都成立的真话，不许删）",
     hint9b.indexOf("已锁定") >= 0 && hint9b.indexOf("本次不改") >= 0
     && hint9b.indexOf("跑完再改") >= 0, hint9b);
  ck("9b 共用锁定提示不再代办「可离开」（两条链结论相反）",
     hint9b.indexOf("可离开") < 0, hint9b);
  // 9b1 P3-新1：尾句「进度条会显示已用时」只对**错词重跑**成立——重新成稿是同步
  //      fetch（无进度条）、批量重试只在 say 行里报 n/N（也无已用时），属越界指针。
  ck("9b1 共用锁定提示不得再指进度条/已用时（只对单链成立的越界指针）",
     hint9b.indexOf("进度条") < 0 && hint9b.indexOf("已用时") < 0, hint9b);
  // 9b2 对照：错词重跑那条是**服务端后台任务**（202＋job_id，重载可接管），
  //     「可离开」属实 → 必须仍在，证明 9a/9b 不是一刀切删掉真话
  reset();
  renderVocabApplyProgress({state: "running", stage: "rerunning", rerun_old: true,
    total: 2, done: 1, elapsed_seconds: 5});
  ck("9b2 对照：错词重跑进度条仍保留「可离开」（服务端任务，属实）",
     (REC.text["candidateApplyText"] || "").indexOf("可离开本页面") >= 0,
     REC.text["candidateApplyText"]);

  // 9c P2-2：跨目录 409（本页新增的拒绝出口）必须把人话原因透出来
  reset();
  el("inData").value = S9_ROOT_B;
  twoFailed();
  route("/api/retry", {status: 409, json: {ok: false,
    error: "这次重试针对的是另一个数据目录（零执行，未重排任何任务）；"
         + "请先核对页面上方的数据目录与当前监听的目录是否一致，避免误操作别的目录"}},
    "POST");
  retryAllFailed();
  await settle(6);
  var say9c = SAYS.join(" | "), hint9c = REC.text["recoverHint"] || "";
  ck("9c 失败不再只显示一个数字（收尾说「失败 N 个」并指向原因）",
     say9c.indexOf("失败 2 个") >= 0 && say9c.indexOf("原因见下方提示") >= 0, SAYS);
  ck("9c 后端人话原因落到首屏恢复区（旧形态零回显）",
     hint9c.indexOf("另一个数据目录") >= 0, hint9c);
  ck("9c 回显不含真实数据目录（D-12，前后端都不吐路径）",
     hint9c.indexOf(S9_ROOT_B) < 0 && say9c.indexOf(S9_ROOT_B) < 0,
     [hint9c, say9c]);

  // 9d 404（任务不存在）同样透出，不因代码不同而静默
  reset();
  el("inData").value = S9_ROOT_A;
  cache.runs = [{run_id: "run-1", status: "FAILED"}];
  route("/api/retry", {status: 404, json: {ok: false, error: "任务不存在，请刷新后重试"}},
        "POST");
  retryAllFailed();
  await settle(6);
  ck("9d 404 也回显原因（不是只看到「失败请求 1」）",
     (REC.text["recoverHint"] || "").indexOf("任务不存在") >= 0,
     REC.text["recoverHint"]);

  // 9e 部分成功：成功的照常计入已排队，失败的原因照样透出（互不顶掉）
  reset();
  el("inData").value = S9_ROOT_A;
  cache.runs = [{run_id: "run-1", status: "FAILED"},
                {run_id: "run-2", status: "FAILED"},
                {run_id: "run-3", status: "FAILED"}];
  FETCH_QUEUE.push({status: 202, json: {ok: true}});
  FETCH_QUEUE.push({status: 409, json: {ok: false,
    error: "这次重试针对的是另一个数据目录（零执行，未重排任何任务）"}});
  FETCH_QUEUE.push({status: 202, json: {ok: true}});
  retryAllFailed();
  await settle(8);
  ck("9e 部分成功：已排队 2/3 且失败原因仍回显",
     SAYS.join(" | ").indexOf("已排队 2/3") >= 0
     && (REC.text["recoverHint"] || "").indexOf("另一个数据目录") >= 0,
     [SAYS, REC.text["recoverHint"]]);

  // 9f P2-2 本地前置门：框里是相对路径 → 整批零请求（旧形态 N 条都白撞后端 400）
  reset();
  el("inData").value = "relative/x";
  effectiveDataRoot = "";
  twoFailed();
  retryAllFailed();
  await settle(4);
  ck("9f 非法目录：一个请求都不发（白跑被本地拦下）",
     retryCalls().length === 0, retryCalls().length);
  ck("9f 非法目录：人话口径与后端 400 同句（数据目录须为绝对路径）",
     SAYS.join(" | ").indexOf("数据目录须为绝对路径") >= 0
     && (REC.text["recoverHint"] || "").indexOf("数据目录须为绝对路径") >= 0,
     [SAYS, REC.text["recoverHint"]]);
  ck("9f retryBody 非法目录回 null（不被当成「不带 data_root」放行）",
     retryBody("run-1") === null, retryBody("run-1"));

  // 9g 单条重试同一道门；9h 对照：目录合法时照常发，不误伤正常路径
  reset();
  el("inData").value = "relative/x";
  effectiveDataRoot = "";
  var n9g = REC.calls.length;
  retryRun("run-1");
  await settle(2);
  ck("9g 单条重试遇相对目录：不发请求且给人话",
     REC.calls.length === n9g && SAYS.join(" | ").indexOf("重试未发起") >= 0,
     [REC.calls.length - n9g, SAYS]);

  reset();
  el("inData").value = S9_ROOT_A;
  route("/api/retry", {status: 202, json: {ok: true}}, "POST");
  retryRun("run-1");
  await settle(3);
  ck("9h 对照：目录合法时单条重试照常 POST 且带 data_root",
     retryCalls().length === 1
     && retryCalls()[0].body.indexOf('"data_root":"' + S9_ROOT_A + '"') >= 0,
     retryCalls().map(function(c){ return c.body; }));
}

// ------------- S10 P1-4/FR-3/HD-2=A：脱敏摘要复制＋页面逐条本地证据
// 反向证伪①：把 redactPathField/redactPathTail/redactCopyField 打成恒等（＝去掉脱敏）
//             → 10b 必须 rc=1；
// 反向证伪②：把 copyAllFailedReasons 的 shortOk 换成整篇正文（＝长文进状态行）
//             → 10a/10d 必须 rc=1；
// 反向证伪③（P2-1）：把 loadDiag 的 mark 改回「写模块级 diagCache」旧形态
//             → 10f 必须 rc=1（A 目录的迟到响应会落到 B 目录的缓存里）；
// 反向证伪④（P2-2 前端侧）：拿掉 evidenceLinesFor 里替代路径那行的 redactPathField
//             （复核报告记的 :1367，行号随后续改动漂移，以字段为准）
//             → 10g 必须 rc=1（未打码回包的真路径会进页面证据）。
var S10_ROOT = "/tmp/p14-fake-rootA";
var REAL_USER_PATH = "/Users/zzymima0000/需转录视频/第七周/样例视频.mp4";
var REAL_ALT_PATH = "/Users/zzymima0000/暂不转录视频/第七周/样例视频.mp4";
function diagReqs(){
  var n = 0;
  for(var i = 0; i < REC.urls.length; i++){
    if(REC.urls[i].indexOf("/api/failures/diagnosis") >= 0) n++;
  }
  return n;
}
function p14Item(extra){
  var base = {run_id: "run-1", source_label: "样例视频.mp4", source_dir_tail: "…/第七周",
    raw_error_code: "SOURCE_NOT_AT_RECORDED_PATH", action_category: "SOURCE_LOCATION_REVIEW",
    confidence: "HIGH", missing_evidence: "页面同刻 snapshot/state event 链",
    next_action: "确认替代路径后重新诊断",
    root_cause: "SOURCE_NOT_AT_RECORDED_PATH+IDENTITY_MATCH_AT_ALTERNATE_PATH",
    stage: "DISCOVERY", recorded_path_redacted: "…/需转录视频/第七周/样例视频.mp4",
    recorded_path_exists: false,
    alternate_path_redacted: "…/暂不转录视频/第七周/样例视频.mp4", identity_match: "MATCH",
    persisted_state: "QUEUED", display_state: "FAIL", provenance_status: "NOT_TIME_ALIGNED",
    recovery_eligibility: "NEEDS_HUMAN", will_call_whisper: false,
    evidence_sources: ["state.db", "source filesystem", "job manifest"],
    evidence_conflicts: ["页面快照与持久状态事件未对齐"],
    artifact_presence: {raw_path: false, normalized_path: false, rendered_path: false,
                        canonical_output_path: false}};
  for(var k in (extra || {})){ base[k] = extra[k]; }
  return base;
}
function diagPayload(items){
  return {status: 200, json: {ok: true, diagnosis_version: "v1.3-failure-taxonomy-1",
    diagnosis_snapshot_id: "diag-abc123", generated_at: "2026-09-15T00:00:00Z",
    counts: {}, categories: [], items: items}};
}
function twoFailedRuns(){
  cache.runs = [{run_id: "run-1", status: "FAILED", source_filename: "样例视频.mp4",
                 source_dir_tail: "…/第七周"},
                {run_id: "run-2", status: "FAILED", source_filename: "第二个.mp4",
                 source_dir_tail: "…/第八周"}];
}
// 摘要正文只允许「标题＋逐条白名单字段行」，多一行都不行
function onlyWhitelistedLines(text){
  var lines = String(text).split("\n").slice(1);   // 第 0 行是文档标题
  for(var i = 0; i < lines.length; i++){
    var l = lines[i];
    if(!l) continue;
    if(/^\d+\. /.test(l)) continue;
    if(/^ {3}(错误码|类别|置信度|缺失证据|建议)：/.test(l)) continue;
    return false;
  }
  return true;
}
function countOf(text, needle){
  return String(text).split(needle).length - 1;
}
function hasAnyAbsPath(text){
  var t = String(text);
  var bad = ["/Users/", "/Volumes/", "/private/", "/var/", "/tmp/", "/home/"];
  for(var i = 0; i < bad.length; i++){ if(t.indexOf(bad[i]) >= 0) return bad[i]; }
  return "";
}
function shortSay(){ return SAYS[SAYS.length - 1] || ""; }
async function s10(){
  // 10a 诊断可得：7 类字段齐、脱敏来源/目录尾段都在、真实路径不在
  reset();
  el("inData").value = S10_ROOT;
  twoFailedRuns();
  route("/api/failures/diagnosis",
        diagPayload([p14Item({}), p14Item({run_id: "run-2", source_label: "第二个.mp4",
                                          source_dir_tail: "…/第八周"})]));
  copyAllFailedReasons();
  ck("10a 复制前先出短提示（不是静默等）",
     SAYS.join(" | ").indexOf("正在整理脱敏摘要") >= 0, SAYS);
  await settle(4);
  var t = CLIP[CLIP.length - 1] || "";
  ck("10a 摘要有正文（真的写进剪贴板）", t.length > 80, t.length);
  ck("10a 每条 7 类字段齐全（各出现 N 次）",
     countOf(t, "错误码：") === 2 && countOf(t, "类别：") === 2
     && countOf(t, "置信度：") === 2 && countOf(t, "缺失证据：") === 2
     && countOf(t, "建议：") === 2, t);
  ck("10a 脱敏 source label ＋ 目录尾段都在",
     t.indexOf("样例视频.mp4（目录尾段：…/第七周）") >= 0
     && t.indexOf("第二个.mp4（目录尾段：…/第八周）") >= 0, t);
  ck("10a 取值来自诊断接口（不编造类别/置信度/错误码）",
     t.indexOf("SOURCE_LOCATION_REVIEW") >= 0 && t.indexOf("HIGH") >= 0
     && t.indexOf("SOURCE_NOT_AT_RECORDED_PATH") >= 0, t);
  ck("10a 后端词汇字段不被路径脱敏误伤（缺失证据原样）",
     t.indexOf("缺失证据：页面同刻 snapshot/state event 链") >= 0, t);
  ck("10a 摘要里零绝对路径（/Users//Volumes//private//var//tmp/ 扫描）",
     hasAnyAbsPath(t) === "", hasAnyAbsPath(t));
  ck("10a 摘要里没有转写正文标记（只带元数据）",
     t.indexOf("正文MARKER") < 0, t);
  ck("10a 复制成功提示是一句短话（无换行、≤40 字）",
     shortSay().indexOf("\n") < 0 && shortSay().length <= 40 && shortSay().indexOf("已复制 2 条脱敏摘要") >= 0,
     shortSay());
  ck("10a toast 同样是短句（不弹长文洪水）",
     TOASTS.join("|").indexOf("\n") < 0
     && TOASTS.every(function(x){ return x.length <= 40; }), TOASTS);
  ck("10a 长文不得进状态提示行",
     SAYS.join(" | ").indexOf("缺失证据：") < 0, SAYS);
  ck("10a 复制不自动打开长文面板（FR-17）", !REC.cls["longPanel"], REC.cls["longPanel"]);
  ck("10a 长文只进短提示背后的可关闭面板备用入口",
     el("btnViewLong").style.display === "inline-block"
     && String(lastLongText).indexOf("缺失证据：") >= 0
     && String(lastLongText).length === t.length, String(lastLongText).length);
  var nDiag = 0;
  for(var i = 0; i < REC.urls.length; i++){
    if(REC.urls[i].indexOf("/api/failures/diagnosis") >= 0) nDiag++;
  }
  ck("10a 同一轮只调一次诊断接口（读盘有缓存，不反复扫盘）", nDiag === 1, nDiag);
  ck("10a 诊断请求带上本页生效数据目录",
     (REC.urls[REC.urls.length - 1] || "").indexOf("data_root=") >= 0, REC.urls);

  // 10b 兜底脱敏：诊断字段里混进真实绝对路径（旧版/异常后端），摘要里也不许出现
  reset();
  el("inData").value = S10_ROOT;
  twoFailedRuns();
  route("/api/failures/diagnosis", diagPayload([p14Item({
    source_label: REAL_USER_PATH,
    source_dir_tail: "/Users/zzymima0000/暂不转录视频/第七周",
    next_action: "去 " + REAL_USER_PATH + " 看看文件是否还在"})]));
  copyAllFailedReasons();
  await settle(4);
  var t2 = CLIP[CLIP.length - 1] || "";
  ck("10b 真实绝对路径不进摘要（去掉脱敏即 rc=1）",
     t2.indexOf("/Users/") < 0 && t2.indexOf(REAL_USER_PATH) < 0, t2);
  ck("10b 打码后仍留可用信息（不是全抹成空）",
     t2.indexOf("…/需转录视频/第七周/样例视频.mp4") >= 0, t2);
  ck("10b 来源 label 按 _diag_redact_path 同口径（basename＋两级父目录）",
     countOf(t2, "1. …/需转录视频/第七周/样例视频.mp4") === 1
     && countOf(t2, "/Users/") === 0, t2);
  ck("10b 目录尾段按 _dir_tail 同口径（…/末段）",
     t2.indexOf("（目录尾段：…/第七周）") >= 0, t2);
  ck("10b 建议里整句路径按 _strip_paths 同口径抹掉（宁可多抹不漏尾段）",
     t2.indexOf("去 …") >= 0 && t2.indexOf("看看文件是否还在") < 0, t2);

  // 10c 页面可看逐条本地证据（DoD3）：详情区内联 ＋ 逐条面板，都不只藏在复制结果里
  reset();
  el("inData").value = S10_ROOT;
  twoFailedRuns();
  route("/api/failures/diagnosis",
        diagPayload([p14Item({}), p14Item({run_id: "run-2", source_label: "第二个.mp4"})]));
  renderDiagEvidence("run-1");
  ck("10c 详情区先给「加载中」不改写事实", (REC.text["diagEvidence"] || "").indexOf("加载中") >= 0,
     REC.text["diagEvidence"]);
  await settle(4);
  var ev = REC.text["diagEvidence"] || "";
  ck("10c 详情区逐条本地证据在页面上可见",
     ev.indexOf("原登记路径（脱敏）：…/需转录视频/第七周/样例视频.mp4") >= 0
     && ev.indexOf("身份匹配：MATCH") >= 0, ev);
  ck("10c 页面证据含冲突/provenance/恢复资格（比复制摘要更全）",
     ev.indexOf("provenance：NOT_TIME_ALIGNED") >= 0 && ev.indexOf("恢复资格：NEEDS_HUMAN") >= 0
     && ev.indexOf("证据冲突：页面快照与持久状态事件未对齐") >= 0, ev);
  ck("10c 页面证据里也零绝对路径", hasAnyAbsPath(ev) === "", hasAnyAbsPath(ev));
  // 10c2 详情区每 5 秒随 refresh 重画：已查过的快照不得再写回「加载中」（否则每 5 秒闪一下）
  renderDiagEvidence("run-1");
  ck("10c2 刷新重画详情区不再闪「加载中」（同步返回时旧证据仍在）",
     (REC.text["diagEvidence"] || "").indexOf("加载中") < 0
     && (REC.text["diagEvidence"] || "").indexOf("身份匹配：MATCH") >= 0,
     REC.text["diagEvidence"]);
  var nDiag3 = 0;
  for(var q = 0; q < REC.urls.length; q++){
    if(REC.urls[q].indexOf("/api/failures/diagnosis") >= 0) nDiag3++;
  }
  ck("10c2 重画不重复请求诊断（同目录同轮复用缓存）", nDiag3 === 1, nDiag3);
  showFailEvidence();
  ck("10c 逐条面板先开再填（读取中占位，不假装已读）",
     REC.cls["longPanel"] === "open"
     && (REC.text["longPanelText"] || "").indexOf("读取中") >= 0, REC.text["longPanelText"]);
  await settle(4);
  var pv = REC.text["longPanelText"] || "";
  ck("10c 逐条面板列出全部失败项（逐条可看）",
     pv.indexOf("失败本地证据（逐条，只读；共 2 条") >= 0
     && pv.indexOf("run …run-1") >= 0 && pv.indexOf("run …run-2") >= 0, pv);
  ck("10c 面板正文零绝对路径", hasAnyAbsPath(pv) === "", hasAnyAbsPath(pv));
  ck("10c 面板是既有可关闭面板（四退出复用，未新造浮层）",
     !!el("longPanel") && !!el("btnLongClose"), REC.cls["longPanel"]);

  // 10d 降级路：诊断不可得 → 缺失写「未提供」不编造；原因/正文/token 一律不进摘要
  reset();
  el("inData").value = S10_ROOT;
  detailsByRun = {"run-1": {state: "FAIL",
    verdict: "源视频文件找不到了：样例视频.mp4；正文MARKER与TOKEN-ABC一律不得出现→检查 "
             + REAL_USER_PATH + " 是否还在，补回后点重试"}};
  cache.runs = [{run_id: "run-1", status: "FAILED", source_filename: "样例视频.mp4",
                 source_dir_tail: "…/第七周"}];
  route("/api/failures/diagnosis", {status: 200, json: {ok: false, code: "DB_MISSING"}});
  copyAllFailedReasons();
  await settle(4);
  var t4 = CLIP[CLIP.length - 1] || "";
  ck("10d 诊断不可得仍给摘要（降级不空手）",
     t4.indexOf("样例视频.mp4") >= 0 && t4.indexOf("失败诊断摘要（脱敏；共 1 条）") >= 0, t4);
  ck("10d 缺失字段写「未提供」，不编造类别/置信度/错误码",
     t4.indexOf("错误码：未提供（诊断不可用）") >= 0
     && t4.indexOf("类别：未提供（诊断不可用）") >= 0
     && t4.indexOf("置信度：未提供（诊断不可用）") >= 0
     && t4.indexOf("缺失证据：未提供（诊断不可用）") >= 0, t4);
  ck("10d 降级建议里的真实绝对路径已脱敏（抹到中文标点收尾）",
     t4.indexOf("/Users/") < 0 && t4.indexOf("检查 …，补回后点重试") >= 0, t4);
  ck("10d 原因/正文/token 不进摘要（只留 7 类允许字段）",
     t4.indexOf("正文MARKER") < 0 && t4.indexOf("TOKEN-ABC") < 0, t4);
  ck("10d 摘要正文只有白名单字段行（白名单外键没有出口）",
     onlyWhitelistedLines(t4) === true, t4);
  ck("10d 白名单恰好是 FR-3 那 7 类字段",
     FAIL_DIGEST_FIELDS.join(",") === "label,tail,code,category,confidence,missing,next",
     FAIL_DIGEST_FIELDS.join(","));
  ck("10d 降级路成功提示仍是短句", shortSay().indexOf("\n") < 0 && shortSay().length <= 40
     && shortSay().indexOf("已复制 1 条脱敏摘要") >= 0, shortSay());
  renderDiagEvidence("run-1");
  await settle(4);
  ck("10d 诊断不可用时页面证据如实说明降级（不装作有证据）",
     (REC.text["diagEvidence"] || "").indexOf("诊断不可用") >= 0
     && hasAnyAbsPath(REC.text["diagEvidence"] || "") === "",
     REC.text["diagEvidence"]);

  // 10e 没有目录 / 非法目录：不查诊断（宁可不给，也不去诊断别的目录）
  reset();
  el("inData").value = "relative/x";
  effectiveDataRoot = "";
  cache.runs = [{run_id: "run-1", status: "FAILED", source_filename: "样例视频.mp4"}];
  copyAllFailedReasons();
  await settle(4);
  var nDiag2 = 0;
  for(var j = 0; j < REC.urls.length; j++){
    if(REC.urls[j].indexOf("/api/failures/diagnosis") >= 0) nDiag2++;
  }
  ck("10e 数据目录非法/缺失时不发诊断请求（不误连别的目录）", nDiag2 === 0, REC.urls);
  ck("10e 该情形仍给降级摘要且不含路径",
     (CLIP[CLIP.length - 1] || "").indexOf("未提供（诊断不可用）") >= 0
     && hasAnyAbsPath(CLIP[CLIP.length - 1] || "") === "", CLIP[CLIP.length - 1]);
  // 10f P2-1 反例：A 目录诊断在飞 → 用户切到目录 B ＋ 手动刷新（:2028 dropDiagCache）
  //     → 放行 A 的迟到响应 → 取到的必须是 B 的数据或明确未加载，绝不能是 A 的 items
  reset();
  var RACE_A = "/tmp/p14-race-A", RACE_B = "/tmp/p14-race-B";
  el("inData").value = RACE_A;
  cache.runs = [{run_id: "run-1", status: "FAILED", source_filename: "a.mp4"}];
  HOLD.push({match: "p14-race-A", queue: []});
  loadDiag();                                   // A 的请求发出后挂住（还在飞）
  ck("10f A 目录诊断请求已在飞", diagReqs() === 1, diagReqs());
  el("inData").value = RACE_B;
  dropDiagCache();                              // ＝手动刷新 :2028
  route("/api/failures/diagnosis", diagPayload([p14Item({source_label: "B-ITEM.mp4"})]));
  loadDiag();                                   // 换目录后必须能重新发起
  await settle(3);
  ck("10f 换目录＋刷新后能重新发起诊断（不因旧请求在飞就永久算「已加载」）",
     diagReqs() === 2, diagReqs());
  ck("10f 新目录的诊断先落地（B 的数据在缓存里）",
     (diagItemFor("run-1") || {}).source_label === "B-ITEM.mp4",
     diagItemFor("run-1") && diagItemFor("run-1").source_label);
  HOLD[0].queue[0].release({status: 200, json: {ok: true, diagnosis_snapshot_id: "diag-A",
    generated_at: "2026-09-15T00:00:00Z", items: [p14Item({source_label: "A-ITEM.mp4"})]}});
  await settle(4);
  var raced = diagItemFor("run-1");
  ck("10f 迟到响应不得落到新目录的缓存（绝不能是 A 的 items）",
     !raced || String(raced.source_label) !== "A-ITEM.mp4", raced && raced.source_label);
  ck("10f 迟到响应也不得改写给到别的目录的快照 id",
     String(diagCache.snap || "") !== "diag-A" && String(diagCache.root || "") === RACE_B,
     [diagCache.snap, diagCache.root]);
  copyAllFailedReasons();
  await settle(4);
  var t6 = CLIP[CLIP.length - 1] || "";
  ck("10f 摘要正文里零 A 目录数据（换目录后的复制不串味）",
     t6.indexOf("A-ITEM") < 0 && t6.indexOf("B-ITEM") >= 0, t6);

  // 10f2 同一反例的另一形态：不点刷新、只把目录改成 B → 也必须重新发起（不吃旧 pending）
  reset();
  el("inData").value = RACE_A;
  cache.runs = [{run_id: "run-1", status: "FAILED", source_filename: "a.mp4"}];
  HOLD.push({match: "p14-race-A", queue: []});
  loadDiag();
  el("inData").value = RACE_B;
  route("/api/failures/diagnosis", diagPayload([p14Item({source_label: "B-ITEM.mp4"})]));
  loadDiag();
  await settle(3);
  ck("10f2 只换目录（未刷新）也重新发起诊断，不吃旧目录的 pending",
     diagReqs() === 2, diagReqs());
  HOLD[0].queue[0].release({status: 200, json: {ok: true, items: [p14Item({source_label: "A-ITEM.mp4"})]}});
  await settle(4);
  var raced2 = diagItemFor("run-1");
  ck("10f2 迟到响应同样不串目录", !raced2 || String(raced2.source_label) !== "A-ITEM.mp4",
     raced2 && raced2.source_label);

  // 10g P2-2（前端侧）：诊断回包里的 recorded/alternate 是**未打码**的真实绝对路径
  //     （旧版后端或异常回包）→ 页面证据仍不许出现真路径（拿掉兜底即 rc=1）
  reset();
  el("inData").value = S10_ROOT;
  twoFailedRuns();
  route("/api/failures/diagnosis", diagPayload([p14Item({
    recorded_path_redacted: REAL_USER_PATH,
    alternate_path_redacted: REAL_ALT_PATH})]));
  renderDiagEvidence("run-1");
  await settle(4);
  var ev2 = REC.text["diagEvidence"] || "";
  ck("10g 未打码的替代路径在页面上仍被脱敏（拿掉该行兜底脱敏即 rc=1）",
     ev2.indexOf("替代路径（脱敏）：…/暂不转录视频/第七周/样例视频.mp4") >= 0
     && ev2.indexOf(REAL_ALT_PATH) < 0, ev2);
  ck("10g 未打码的原登记路径同样被脱敏", ev2.indexOf(REAL_USER_PATH) < 0
     && ev2.indexOf("原登记路径（脱敏）：…/需转录视频/第七周/样例视频.mp4") >= 0, ev2);
  ck("10g 该情形页面证据里零真实绝对路径", hasAnyAbsPath(ev2) === "", hasAnyAbsPath(ev2));
  showFailEvidence();
  await settle(4);
  var pv2 = REC.text["longPanelText"] || "";
  ck("10g 逐条面板里替代/原登记路径同样已被脱敏",
     pv2.indexOf(REAL_ALT_PATH) < 0 && pv2.indexOf(REAL_USER_PATH) < 0
     && hasAnyAbsPath(pv2) === "", hasAnyAbsPath(pv2));
}

// ------------------------------------------------------------------ S11 P1-5 词库与候选易用性
// 按 `data-candidate-*` 从**真源码渲染出来的 markup** 反解勾选项：勾选初态断言认的是
// loadVocabCandidates 的真实输出，不是手抄常量（改回旧形态即 rc=1）。
// P1-5 返工（code-reviewer P3-4）：桩要有一份**可持久的 DOM 影子**——markup 首次解析出
// 元素对象后按 key 记住，此后每次 querySelectorAll 返回**同一批对象**，于是
// `syncCandidateSelectAll` 对 `g.checked` 的写回落在**断言读到的那份状态**上（真实 DOM 里
// 改的是 property、再次 query 拿回的是同一个元素）。只有重写 innerHTML（真实 DOM 会销毁
// 子节点）才作废重建。旧形态每次从 markup 重新 new 临时对象，写回全丢、断言读到的是
// markup 默认的 checked 属性 → M9（删组头回写）rc=0，属假信心。
function candListStub(){
  var reg = {}, html = "";
  function mk(idx, level, checked){
    return {checked: checked, disabled: false, onclick: null, onchange: null,
            getAttribute: function(k){
              return k === "data-candidate-index" ? idx : level; }};
  }
  function pick(key, idx, level, checked){
    if(!reg[key]) reg[key] = mk(idx, level, checked);
    return reg[key];
  }
  var box = {querySelectorAll: function(sel){
    var tags = html.match(/<input[^>]*>/g) || [], out = [], i, m;
    for(i = 0; i < tags.length; i++){
      if(sel === "[data-candidate-index]"){
        m = /data-candidate-index="([^"]*)"[^>]*data-candidate-level="([^"]*)"/.exec(tags[i]);
        if(m) out.push(pick("i:" + m[1], m[1], m[2], / checked[ >]/.test(tags[i])));
      }else if(sel === "[data-candidate-group]"){
        m = /data-candidate-group="([^"]*)"/.exec(tags[i]);
        if(m) out.push(pick("g:" + m[1], m[1], m[1], / checked[ >]/.test(tags[i])));
      }
    }
    return out;
  }};
  Object.defineProperty(box, "innerHTML", {
    get: function(){ return html; },
    set: function(v){ html = String(v); reg = {}; }});   // 重写 innerHTML＝旧子节点全销毁
  return box;
}

async function s11(){
  // ---- A2/B `VOCAB-FOLD P3-2`：分组判定只做展示层归一化（trim＋小写）
  reset();
  ck("S11a 分组判定去前后空格：`  programming  ` 归预置-编程",
     vocabGroupFor({source: "  programming  "}) === "programming",
     vocabGroupFor({source: "  programming  "}));
  ck("S11a 分组大小写不敏感：`Crypto` 归预置-币圈",
     vocabGroupFor({source: "Crypto"}) === "crypto");
  ck("S11a preset 前缀带空格同组：` preset:finance ` 归预置-金融",
     vocabGroupFor({source: " preset:finance "}) === "finance");
  ck("S11a 纯空白/无 source/`candidate`/null 一律归「我自己加的」",
     vocabGroupFor({source: "   "}) === "user" && vocabGroupFor({}) === "user"
     && vocabGroupFor({source: "candidate"}) === "user" && vocabGroupFor(null) === "user");

  // ---- A1/B P3-1 行内来源标签限四组 ＋ A4/B P3-4 筛选计数 ＋ trim 不许碰落盘串
  reset();
  loadedVocabEntries = [
    {wrong: "候选错词", right: "候选正词", source: "candidate"},
    {wrong: " 带空格错词 ", right: " 带空格正词 ", source: " finance "},
    {wrong: "野生错词", right: "野生正词", source: "whatever"}
  ];
  el("vocabFilter").value = "";
  renderVocabList();
  var vhtml = el("vocabList").innerHTML;
  ck("S11b 行内来源标签恒为四组标签之一（candidate/领域名/野生值都不外露）",
     vhtml.indexOf("（来源：我自己加的）") >= 0
     && vhtml.indexOf("（来源：预置-金融）") >= 0
     && vhtml.indexOf("candidate") < 0 && vhtml.indexOf("whatever") < 0
     && vhtml.indexOf("finance") < 0, vhtml);
  ck("S11b 未过滤时 summary 只报总数（不出现「匹配」）",
     el("vocabListSummary").textContent === "已导入词库（3条）",
     el("vocabListSummary").textContent);
  ck("S11c 展示层 trim 不碰落盘串：删除按钮仍带原样错词（含前后空格）",
     vhtml.indexOf('data-vocdel=" 带空格错词 "') >= 0, vhtml);

  el("vocabFilter").value = "错词";
  renderVocabList();
  ck("S11d 过滤中 summary 同时报总数与匹配数",
     el("vocabListSummary").textContent === "已导入词库（3条）· 匹配 3 条",
     el("vocabListSummary").textContent);
  el("vocabFilter").value = "候选";
  renderVocabList();
  ck("S11d 匹配数随过滤缩小（总数不变）",
     el("vocabListSummary").textContent === "已导入词库（3条）· 匹配 1 条",
     el("vocabListSummary").textContent);

  // ---- A3/B P3-3：空库整块一句；过滤零命中仍按组显示（口径未变）
  reset();
  loadedVocabEntries = [];
  el("vocabFilter").value = "";
  renderVocabList();
  var vempty = el("vocabList").innerHTML;
  ck("S11e 空库只留一句人话（无四个组块、无四连「暂无匹配词条」）",
     vempty.indexOf("vocabGroup") < 0 && vempty.indexOf("暂无匹配词条") < 0
     && vempty.indexOf('class="hint"') >= 0, vempty);
  // P1-5 返工（code-reviewer P3-3）：空态文案**整条比对**（不用子串——子串挡不住方位词走样），
  // 钉死「只描述动作、不写方位」。写回「在上方手动添加」而添加行实际在列表**下方**
  // （`index.html:268-271` vs 列表块 `:263-267`）即事实错误，本仓已因此返工两次。
  ck("S11e 空库空态文案整条一致（不含方位词，改回「在上方手动添加」即 rc=1）",
     vempty === '<div class="hint">词库还没有词条：跑一次 AI 审查生成候选，或手动添加词条。</div>',
     vempty);

  reset();
  loadedVocabEntries = [{wrong: "有的错词", right: "有的正词", source: "user"}];
  el("vocabFilter").value = "没有这一条";
  renderVocabList();
  var vnone = el("vocabList").innerHTML;
  ck("S11e 过滤零命中仍按四组显示（未被空态分支吃掉）",
     (vnone.match(/class="vocabGroup"/g) || []).length === 4
     && (vnone.match(/暂无匹配词条/g) || []).length === 4, vnone);

  // ---- A5/B `CANDIDATE-UI2 P3-3`：组头勾选不再顺带折叠
  reset();
  ELS["candidateList"] = candListStub();
  FETCH_QUEUE.push({status: 200, json: {ok: true, has_candidates: true, count: 2,
      candidates_revision: "REV-S11", data_root: "/tmp/p12-s11",
      candidates: {high: [{index: 0, wrong: "高错词", right: "高正词"}],
                   medium: [{index: 1, wrong: "中错词", right: "中正词"}], low: []}}});
  loadVocabCandidates();
  await flush();
  var cheads = el("candidateList").innerHTML.match(/<label class="candGroupHead"[^>]*>/g) || [];
  ck("S11f 三个组头都掐断冒泡（点勾选只勾选、不再折叠）",
     cheads.length === 3
     && cheads.every(function(t){ return t.indexOf("stopPropagation") >= 0; }), cheads);
  // ---- A6/B `CANDIDATE-UI2 P3-4`：全选初态与组内勾选同源
  ck("S11g 全选初态＝真状态（高中组默认已勾、无低置信度 → 全选勾上）",
     el("candidateSelectAll").checked === true, el("candidateSelectAll").checked);
  ck("S11g 组头初态同样按真勾选算（high/medium 勾、low 不勾）",
     candidateGroupCheckboxes().map(function(g){
       return g.getAttribute("data-candidate-group") + ":" + g.checked; }).join(",")
     === "high:true,medium:true,low:false",
     candidateGroupCheckboxes().map(function(g){
       return g.getAttribute("data-candidate-group") + ":" + g.checked; }));
  // P1-5 返工（code-reviewer P3-4）：上面那条的期望值与本夹具的 markup 默认**恰好重合**
  // （high/medium 的组头 markup 就是 checked、low 不是），所以它单独**验不到组头回写**。
  // 这条用一个**空组**把两者拆开：medium 0 条 → 真值 false（`items.length>0`），而 markup
  // 默认给的是 checked → 只有真跑过 syncCandidateSelectAll 的组头回写才过得去（删即 rc=1）。
  var citems0 = candidateCheckboxes();
  citems0[0].checked = false;          // 行为级：用户取消一个已勾项
  citems0[0].onchange();               // 真源码 :1613 绑的就是 syncCandidateSelectAll
  ck("S11g 取消已勾项后组头与全选跟着取消（组头回写被删即 rc=1）",
     candidateGroupCheckboxes().map(function(g){
       return g.getAttribute("data-candidate-group") + ":" + g.checked; }).join(",")
     === "high:false,medium:true,low:false" && el("candidateSelectAll").checked === false,
     [el("candidateSelectAll").checked,
      candidateGroupCheckboxes().map(function(g){
        return g.getAttribute("data-candidate-group") + ":" + g.checked; })]);

  reset();
  ELS["candidateList"] = candListStub();
  FETCH_QUEUE.push({status: 200, json: {ok: true, has_candidates: true, count: 2,
      candidates_revision: "REV-S11", data_root: "/tmp/p12-s11",
      candidates: {high: [{index: 0, wrong: "高错词", right: "高正词"}],
                   medium: [], low: [{index: 1, wrong: "低错词", right: "低正词"}]}}});
  loadVocabCandidates();
  await flush();
  ck("S11g 存在默认不勾的低置信度时全选就是不勾（同一口径，不硬写 false）",
     el("candidateSelectAll").checked === false
     && candidateGroupCheckboxes().filter(function(g){
          return g.getAttribute("data-candidate-group") === "high"; })[0].checked === true,
     [el("candidateSelectAll").checked,
      candidateGroupCheckboxes().map(function(g){ return g.checked; })]);
  // P1-5 返工（code-reviewer P3-4）：空组（medium 0 条）组头真值＝false，markup 默认＝checked
  // → 这条与 markup 默认相反，删掉组头回写即 rc=1（M9 的定点验收断言）。
  ck("S11g 空组组头按 0 条算（真值 false，不是 markup 默认的 checked）",
     candidateGroupCheckboxes().map(function(g){
       return g.getAttribute("data-candidate-group") + ":" + g.checked; }).join(",")
     === "high:true,medium:false,low:false",
     candidateGroupCheckboxes().map(function(g){
       return g.getAttribute("data-candidate-group") + ":" + g.checked; }));
}

// ------------------------------------------------------------------ S12 P1-6 sha256 向量与隐藏键分区
async function s12(){
  reset();
  // 已知向量（Python hashlib 独立算出钉死；写错位运算/常量即挂）
  ck("S12 sha256 向量：abc",
     sha256Hex("abc") === "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
     sha256Hex("abc"));
  ck("S12 sha256 向量：空串",
     sha256Hex("") === "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
     sha256Hex(""));
  ck("S12 sha256 向量：中文 UTF-8",
     sha256Hex("懒得笔记") === "c67e59a03b97fa1a6a165e31d3cc459ed1379c9bcfee97386d85da8dff931034",
     sha256Hex("懒得笔记"));

  // FR-15/HD-7=A：键按 data_root 不可逆摘要分区（sha256 前 8 位），不落原始路径
  el("inData").value = "/tmp/p12-fake-rootA"; effectiveDataRoot = "";
  var kA = hideDoneKey();
  ck("S12 键格式 v2o-hide-done-<8hex>（v2o- 风格一致）",
     /^v2o-hide-done-[0-9a-f]{8}$/.test(kA), kA);
  ck("S12 键＝sha256(data_root) 前 8 位（hashlib 对账）",
     kA === "v2o-hide-done-e6e99aca", kA);
  hideDoneSet(true);
  ck("S12 开关写读一致", hideDoneGet() === true, hideDoneGet());
  el("inData").value = "/tmp/p16-other-root";
  ck("S12 不同 data_root 键不同、状态隔离（反向证伪：串味即挂）",
     hideDoneKey() === "v2o-hide-done-591dad02" && hideDoneGet() === false,
     hideDoneKey());
  el("inData").value = "/tmp/p12-fake-rootA";
  ck("S12 切回原目录状态保留（跨刷新持久语义）", hideDoneGet() === true);
  hideDoneSet(false);
  ck("S12 可逆（取消隐藏即恢复）", hideDoneGet() === false);
  el("inData").value = "";
  effectiveDataRoot = "/tmp/p16-other-root";
  ck("S12 输入框为空时按生效目录（effectiveDataRoot）取键",
     hideDoneKey() === "v2o-hide-done-591dad02", hideDoneKey());
  el("inData").value = "/tmp/p12-fake-rootA"; effectiveDataRoot = "";
  reset();
}

async function s13(){
  // DEVELOP-P1-9：库里已有同名笔记 → 「已跳过（笔记已存在）」，不叫失败、不显示排队中
  reset();
  ck("S13 cnOfState(SKIPPED) 是人话「已跳过（笔记已存在）」",
     cnOfState("SKIPPED") === "已跳过（笔记已存在）", cnOfState("SKIPPED"));
  detailsByRun["r-skip"] = {state: "SKIPPED",
    verdict: "笔记已存在（未覆盖），已跳过转写：/tmp/vault/x.md",
    rendered_path: null, canonical_output_path: "/tmp/vault/x.md"};
  var cnSkip = statusCN({run_id: "r-skip", status: "QUEUED"}, null);
  ck("S13 列表状态＝已跳过（不是排队中）",
     cnSkip === "已跳过（笔记已存在）", cnSkip);
  ck("S13 已跳过不画成失败红（stClass 非 st-bad）",
     stClass(cnSkip) !== "st-bad", stClass(cnSkip));
  ck("S13 已跳过不被判成失败行（批量重试不会带上它）",
     isFailedRun({run_id: "r-skip", status: "QUEUED"}) === false,
     isFailedRun({run_id: "r-skip", status: "QUEUED"}));
  ck("S13 监听脱钩：未监听/监听中同一快照文案一致（FR-13）",
     statusCN({run_id: "r-skip", status: "QUEUED"}, null)
     === statusCN({run_id: "r-skip", status: "QUEUED"},
                  {run_id: "other-run", stage: "听写中"}),
     statusCN({run_id: "r-skip", status: "QUEUED"}, null));
  ck("S13 反向证伪：状态不是 SKIPPED 时仍显示排队中（判定真读了 state）",
     statusCN({run_id: "r-skip", status: "QUEUED"}, null) !== "排队中"
     && (delete detailsByRun["r-skip"],
         statusCN({run_id: "r-skip", status: "QUEUED"}, null) === "排队中"),
     statusCN({run_id: "r-skip", status: "QUEUED"}, null));
  reset();
}

(async function(){
  await s1(); await s2(); await s3(); await s4(); await s5(); await s6(); await s7();
  await s8(); await s9(); await s10(); await s11(); await s12(); await s13();
  if(FAILS.length){ console.log("FRONT FAIL " + FAILS.length + ": " + FAILS.join(" | "));
                    process.exit(1); }
  console.log("FRONT ALL PASS");
})();
"""


def brand_checks():
    """P3-1 品牌断言牙（防改名回退）：只钉用户可见文案；v2o- 键/.v2o class 属技术标识，不在此列；
    start.ps1 一行更严：仅按令牌豁免 V2O_PORT（V2OApp 等子串不再放行）。"""
    root = os.path.dirname(HTML)
    src = open(HTML, encoding="utf-8").read()
    assert '<title>懒得笔记 · 本地视频自动转文字</title>' in src, "title 品牌回退"
    assert '<h1><span class="v2o">懒得笔记</span>' in src, "h1 品牌回退"
    assert 'content:"懒得笔记 · 本机磁带"' in src, "磁带品牌回退"
    # P1-8：`V2O_PORT` 是环境变量技术标识（非用户可见文案），只按 token 边界精确摘除；
    # 不做 V2OApp 之类子串豁免（那是放宽）；V2O_PORT_EXTRA／XV2O_PORT 因边界不符照咬不放。
    sh = open(os.path.join(root, "..", "start.ps1"), encoding="utf-8").read()
    sh_left = [l.strip() for l in sh.splitlines()
               if "V2O" in re.sub(r"\bV2O_PORT\b", "", l)]
    assert not sh_left, "start.ps1 可见文案 V2O 残留（仅豁免 V2O_PORT 令牌）: %s" % sh_left
    mb = open(os.path.join(root, "..", "src", "stage12", "menu_bar.py"), encoding="utf-8").read()
    left = [l.strip() for l in mb.splitlines() if "V2O" in l and "V2OApp" not in l]
    assert not left, "menu_bar.py 可见文案 V2O 残留（非 V2OApp 类名）: %s" % left
    sc = open(os.path.join(root, "..", "src", "stage12", "status_cli.py"), encoding="utf-8").read()
    sc_left = [l.strip() for l in sc.splitlines() if "V2O" in l and "V2OApp" not in l]
    assert not sc_left, "status_cli.py 可见文案 V2O 残留（非 V2OApp 类名）: %s" % sc_left


def layout_checks():
    """P1-6 布局与零回退静态守卫（改坏即 rc=1）。

    FR-10：顶部普通文档流横条（不 sticky/不可收起、旧 220px 列已删、节点有
    文字状态）；FR-11：两列栅格＋窄屏单列断点；FR-12/15：完成区与隐藏开关
    骨架；FR-9：既有功能 DOM/处理入口一个不少；P0-3：批量重跑入口仍唯一。
    """
    src = open(HTML, encoding="utf-8").read()
    bad = []

    def ck(name, cond, extra=""):
        if not cond:
            bad.append("%s  << %s" % (name, extra))

    # ---- FR-10 顶部横条（HD-9=A：普通文档流，滚动自然离开）
    ck("L1 横条 section 存在（flowbar）", 'class="flowbar"' in src)
    ck("L1 磁带容器保留（#tape 由 JS 渲染）", '<div id="tape"></div>' in src)
    ck("L1 不 sticky（反向证伪：加回 sticky 即挂）",
       "position:sticky" not in src and "position: sticky" not in src)
    ck("L1 旧 220px 全高流程列已删（grid-template-columns:220px 不得回来）",
       "grid-template-columns:220px" not in src and ".tape{" not in src
       and 'class="tape"' not in src and "class=\"leader\"" not in src)
    ck("L1 节点带非颜色文字状态（segStateText/.sttxt）",
       "function segStateText(" in src and ".sttxt" in src)
    ck("L1 品牌标签保留在横条（本机磁带）",
       'content:"懒得笔记 · 本机磁带"' in src)

    # ---- FR-11 响应式两列（320px/960px 基线）
    ck("L2 桌面两列栅格（minmax(0,1fr) 300px）",
       "grid-template-columns:minmax(0,1fr) 300px" in src)
    ck("L2 窄屏 960px 折单列", "@media (max-width:960px)" in src)
    ck("L2 单列无横向溢出：栅格子项 min-width:0",
       "section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px;min-width:0}" in src)
    i_bar = src.find('aria-label="流程状态条"')
    i_task = src.find('id="hideDoneChk"')
    i_detail = src.find("选中任务详情")
    ck("L2 DOM 顺序：横条 → 主任务区 → 详情（窄屏单列主操作不落后）",
       0 <= i_bar < i_task < i_detail, (i_bar, i_task, i_detail))

    # ---- FR-12/FR-15 完成区与隐藏开关骨架
    ck("L3 完成区计数条存在（#completedBar）", 'id="completedBar"' in src)
    ck("L3 「展开更早」cursor 追加加载（loadOlderCompleted）",
       "function loadOlderCompleted(" in src
       and "completed_cursor=" in src)
    ck("L3 已完成计数文案（已完成 N 条）", '"已完成 "+total+" 条"' in src)
    ck("L3 隐藏开关在标题区（#hideDoneChk）", i_task > 0)
    ck("L3 隐藏键按 data_root 摘要分区（v2o-hide-done-）",
       '"v2o-hide-done-"+sha256Hex' in src)
    ck("L3 隐藏只改视图过滤（hideOn 滤完成行，零删写）",
       "runs.concat(hideOn?[]:completedRows)" in src)

    # ---- FR-9 零回退：既有功能 DOM 与处理入口一个不少
    for frag, name in [
            ('id="recoverBox"', "恢复与重试区 #recoverBox"),
            ('id="btnRecTranscribe"', "重新转写全部失败按钮"),
            ('id="btnRecReuse"', "从已有文字重新成稿按钮"),
            ('id="btnRecPublish"', "仅重新入库按钮"),
            ('id="btnStart"', "开始监听"), ('id="btnStop"', "停止"),
            ('id="btnRefresh"', "刷新"), ('id="btnClear"', "清空本目录任务"),
            ('id="vocabBox"', "词库区"), ('id="candidateBox"', "候选区"),
            ('复制脱敏摘要', "脱敏摘要复制入口"),
            ('data-retry-publish=', "单条重试入库"), ('data-retry=', "单条重试"),
            ('data-reapply=', "应用新词库重跑"),
            ('function retryRun(', "retryRun"), ('function reapplyOne(', "reapplyOne"),
            ('function publishOnlyRetry(', "publishOnlyRetry"),
            ('function renderVocabList(', "renderVocabList"),
            ('function loadVocabCandidates(', "loadVocabCandidates")]:
        ck("L4 FR-9 保留：%s" % name, frag in src, frag)
    ck("L4 目录浏览 3 个入口（输入/笔记库/数据目录）",
       src.count('class="req"') >= 1 and src.count("data-browse=") == 3,
       src.count("data-browse="))
    # P0-3：全文不得出现第二个批量重跑入口（按钮本体唯一，且无别批量 data-* 挂点）
    ck("L4 P0-3 批量重跑入口唯一（btnRecTranscribe 仅 1 处）",
       src.count('id="btnRecTranscribe"') == 1, src.count('id="btnRecTranscribe"'))
    ck("L4 P0-3 无第二个批量重跑挂点（data-retry-all/btnRetryAll 类）",
       "data-retry-all" not in src and 'id="btnRetryAll"' not in src
       and "data-rerun-all" not in src)

    # ---- 主题默认浅色不动（约束 4）
    ck("L5 默认浅色首帧（data-theme=light）",
       '<html lang="zh-CN" data-theme="light">' in src)
    ck("L5 主题兜底逻辑一字不改",
       'var use=(t==="dark")?"dark":"light";' in src)

    # ---- DEVELOP-P1-9：库里已有同名笔记 → 跳过转写（显示层四处接线，改坏即红）
    ck("L6 statusCN 认 SKIPPED（已跳过（笔记已存在））",
       'if(ws==="SKIPPED")return "已跳过（笔记已存在）";' in src)
    ck("L6 cnOfState 认 SKIPPED",
       'if(s==="SKIPPED")return "已跳过（笔记已存在）";' in src)
    ck("L6 队列文案单列「跳过」（不计入成功/失败）",
       'var skipTxt=(q.skipped||0)>0?(" / 跳过 "+(q.skipped||0)):"";' in src
       and '(q.skipped||0)>0' in src)
    ck("L6 跳过行也给出重试入口（删掉库里笔记后可重跑）",
       'var skippedRow=!!(hasDetail&&d.state==="SKIPPED");' in src
       and 'if(failed||skippedRow){' in src)
    ck("L6 详情区有 SKIPPED 专支（不落「排队等待处理」）",
       'else if(d&&d.state==="SKIPPED"){' in src
       and "已跳过转写（whisper 没跑）" in src)
    ck("L6 入库受阻兜底文案不再指向「检查笔记库权限」",
       "检查笔记库权限后点重试入库" not in src
       and "初稿已保留，入库未完成→检查笔记库权限后点重试" not in src)

    assert not bad, "布局/零回退静态守卫 %d 项失败：%s" % (len(bad), bad)


def main():
    brand_checks()
    print("BRAND SELFTEST PASS")
    layout_checks()
    print("LAYOUT SELFTEST PASS")
    src = open(HTML, encoding="utf-8").read()
    parts = []
    for name in DECLS:
        parts.append(extract_decl(src, name))
    for name in FUNCS:
        parts.append(extract(src, name))
    js = "\n\n".join(parts) + "\n" + DRIVER
    tmp = tempfile.mkdtemp(prefix="p12_front_")
    path = os.path.join(tmp, "harness.js")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(js)
        node = shutil.which("node")
        if not node:
            print("SKIP：本机没有 node，前端桩未跑（如实记账，不冒充通过）")
            return 0
        proc = subprocess.run([node, path], capture_output=True, text=True)
        sys.stdout.write(proc.stdout)
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            print("FRONT SELFTEST FAIL（node rc=%d）" % proc.returncode)
            return 1
        print("FRONT SELFTEST PASS")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
