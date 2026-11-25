/*! 看屏插件的配置页：出表单、收表单、把能改的字段列清楚。
 *
 * 只做三件事：渲染、读请求体、把提交上来的值洗一遍。
 * 洗的逻辑必须在这边，因为页面是明文的，谁都改得了。
 */

/** 可编辑字段：类型、上下限、说明。没列在这里的字段一律拒收。 */
const FIELDS = {
  python: {
    kind: "line",
    label: "Python 解释器",
    hint: "要带 numpy 和 Pillow 的那个 python。留空则依次找环境变量 WHALELIFE_PEEK_PYTHON、PATH 里的 python。Windows 上 PATH 里的 python 常是应用商店的占位符，一跑就报「边车没跑成」，那时把完整路径填进来。",
  },
  seconds: {
    kind: "number", min: 2, max: 60,
    label: "默认抓多久（秒）",
    hint: "一次看屏默认抓几秒。画面不动的时段几乎不留帧，抓长一点并不费事；想让它在长视频里多抓几次变化，就调大。",
  },
  max_frames: {
    kind: "number", min: 1, max: 12,
    label: "一张图最多装几帧",
    hint: "画面变化多时挑几帧拼进同一张图。帧多了细节全，但识图更慢、更费 token；6 是常用值。",
  },
  max_width: {
    kind: "number", min: 400, max: 4096,
    label: "成品图最大宽度",
    hint: "拼好的那张图缩到多宽再送去识图。太宽视觉模型反而看不清细节，1600 一般够用。",
  },
  focus: {
    kind: "select",
    label: "截取范围",
    options: [
      { value: "auto", label: "自动挑区域（推荐）" },
      { value: "content", label: "只送内容区" },
      { value: "whole", label: "整屏不裁" },
    ],
    hint: "自动：先看前台窗口，把变化最集中的一块裁出来；窗口占满全屏、或画面到处在动时退回整屏。只送内容区：强制按变化区裁。整屏不裁：什么都看得到，但一半格子花在标题栏和任务栏上。",
  },
  target: {
    kind: "select",
    label: "抓屏范围",
    options: [
      { value: "screen", label: "整屏（按显示器设置）" },
      { value: "window", label: "只抓前台窗口那一块" },
      { value: "app", label: "只抓指定窗口（进程名或标题）" },
    ],
    hint: "整屏：抓下整块屏幕，坐标就是屏幕像素，适合问「屏幕上有什么」。只抓前台窗口：只抓当前带焦点的那一个窗口，窗口移动、缩放会逐帧跟着走，旁边的东西不进图。只抓指定窗口：按下面那栏填的名字找窗口，后台、被别的窗口挡着也能抓到（游戏、播放器都行）。两种指定方式拿不到窗口时，默认直接报「没抓到」、不出图——要不要退回整屏，由下面那项决定。",
  },
  app: {
    kind: "line",
    label: "抓哪个窗口（进程名或标题片段）",
    hint: "只在上面选了「只抓指定窗口」时起作用。填进程名或窗口标题的一段，不分大小写，比如 horizon-client、chrome、ChatGPT。可以用任务管理器「详细信息」里那个名字；标题就照标题栏抄一段。匹配到多个窗口时取面积最大的那个（主窗口）。",
  },
  fallback_on_miss: {
    kind: "boolean",
    label: "抓不到就退回整屏（默认关）",
    hint: "关着：指定的窗口没抓到就报错停住，一张图都不出。开着：抓不到就换成一整屏继续。默认关着是有意的——你点的是「抓这个窗口」，悄悄换成一整屏，等于把没被要求看的东西也交出去。",
  },
  tool_permission: {
    kind: "select",
    label: "工具权限（ask 每次都要确认 / safe 直接可用）",
    hint: "ask：宿主每次调用前先问一次，谁都不问就给不了，没有审批流的宿主会直接拒。safe：任何会话都能直接抓屏，画面也会交给识图模型。这一项是在「别人偷偷抓屏」和「自己平时用不了」之间选，没有两全。改完要重载插件才生效。",
    options: [
      { value: "ask", label: "ask：每次要确认（默认，没有审批流就用不了）" },
      { value: "safe", label: "safe：直接可用（任何会话都能抓）" },
    ],
  },
  diff_level: {
    kind: "number", min: 1, max: 80,
    label: "变化阈值（灰度差，默认 10）",
    hint: "本地比两帧时，灰度差超过这个数才算「变了」。调小更敏感，光标闪一下也算；调大更钝，只认明显改动。它决定「哪块变了」，不决定留多少帧——那是下面两项的事。",
  },
  cut_abs: {
    kind: "number", min: 0, max: 255,
    label: "单帧突变门槛（0 = 按默认）",
    hint: "一帧变化分超过它就当成「画面一下子换了」直接留。整屏默认 12，认得内容区时默认 2.5。调高只留激烈变化，调低连轻微闪动也留。填 0 用默认。",
  },
  need: {
    kind: "number", min: 0, max: 2000,
    label: "累计积分门槛（0 = 按默认）",
    hint: "变化一点点积累时，攒够这个分才留一帧。整屏默认 144，认得内容区时默认 24。视频那种缓慢流动靠它才留得住。填 0 用默认。",
  },
  tier_style: {
    kind: "select",
    label: "清晰度偏移",
    options: [
      { value: "native", label: "按变化快慢自动定" },
      { value: "up", label: "偏清晰（格子大、帧少）" },
      { value: "down", label: "偏省（格子小、帧多）" },
    ],
    hint: "画面变化慢时格子会自动放大变清晰，变化快时自动缩小以装下更多帧。这一项是整体往哪边偏一档。",
  },
  tier_lock: {
    kind: "number", min: 0, max: 3,
    label: "锁死清晰度档（0 = 不锁）",
    hint: "填 0 交给上一项自动决定。填 1 最清晰，但一张图最多只装 3 帧；填 3 最省，能装到 12 帧。想看清字就填 1，想看整体变化就填 3。",
  },
  grid: {
    kind: "line",
    label: "坐标网格（auto / 0 / 像素步长）",
    hint: "叠在单帧截图上的刻度：每格一条红线，边上的数字是这块屏幕的像素坐标，可以直接说「点 (1480, 320)」或者拿去裁。auto 按画面宽度自己挑步长，0 关掉。多帧拼图不叠（拼图坐标对不回屏幕）。",
  },
  monitors: {
    kind: "line",
    label: "抓哪些显示器（primary / all / 1,3）",
    hint: "只有多显示器时才有区别。primary：只抓主屏（默认）；all：全部抓，未选中的那几块会涂成深灰、保留相对位置，坐标才对得回去；也可以填编号，用逗号隔开，比如 1,3。编号从 1 起，主屏固定是 1，其余按左右顺序排——想看实际编号，对插件目录下的 see_now_sidecar.py 跑一次 --list-monitors。",
  },
  shot_ttl_seconds: {
    kind: "number", min: 0, max: 604800,
    label: "成品图在磁盘上留几秒（0 = 只留本次这张）",
    hint: "拼好的那张成品图留在 shots 目录里多久。0 表示只留本次这张，下次抓屏时被清掉。识别已经在当轮做完，清掉不影响结果。填 3600 是留一小时，604800 是上限（7 天）。",
  },
  pool_ttl: {
    kind: "number", min: 0, max: 604800,
    label: "帧缓存池：留多少秒（0 = 阅后即焚）",
    hint: "多帧抓下来的原始帧，逐张按时间戳存进缓存池。0 表示不落盘，只交出拼好的那张。给个秒数就能事后单独看某一帧，比如 3600 留一小时。",
  },
  pool_max_files: {
    kind: "number", min: 0, max: 5000,
    label: "缓存池最多留几张（0 = 不限）",
    hint: "池里张数到上限后，从最旧的开始删。一次抓屏最多留 12 张，200 大约够二十来次。",
  },
  pool_max_mb: {
    kind: "number", min: 0, max: 20000,
    label: "缓存池最多占多少 MB（0 = 不限）",
    hint: "按体积封顶，同样从最旧的删。一张 1600 宽的帧大约 1-3 MB。",
  },
  crop_min_side_pct: {
    kind: "number", min: 0, max: 90,
    label: "只裁变化区：最小宽或高占比（%，10 = 全屏一成）",
    hint: "只留一帧时，会把变化的那块裁出来单独看，细节比整屏清楚。但变化区如果窄成一条——侧栏在刷、任务栏在闪、光标在跳——裁出来是根面条，反而没法看。宽或高低于这个占比就不裁，退回整屏。0 表示不拦。",
  },
  crop_max_h: {
    kind: "number", min: 120, max: 4000,
    label: "裁剪图最高多少像素（兜底封顶）",
    hint: "形状判据没想到的情况靠这条兜住：裁出来的图超过这个高度就等比缩小，免得交出去一张又细又长的图，token 全花在空处。宽度同时封顶 800。",
  },
  text_layer: {
    kind: "boolean",
    label: "附上屏幕上的文字（慢）",
    hint: "额外提取窗口文字和 OCR 结果，识图更准，但每次要多等几秒。默认关。",
  },
  vision_primary: {
    kind: "select",
    dynamic: "chat",
    lead: [{ value: "chat", label: "跟随当前会话模型（推荐）" }],
    tail: [{ value: "ollama", label: "本地 Ollama（用下面那个模型名）" }],
    label: "一级：用谁识图",
    hint: "默认跟随当前会话：你跟哪个模型说话，就用哪个看图。云端看得快、不占显存，是本机默认。也可以固定成某个对话模型，比如聊天用便宜的、看图用认得清的。",
  },
  vision_backup: {
    kind: "select",
    dynamic: "chat",
    lead: [
      { value: "none", label: "不备用（一级不行就报错）" },
      { value: "chat", label: "跟随当前会话模型" },
    ],
    tail: [{ value: "ollama", label: "本地 Ollama（用下面那个模型名）" }],
    label: "二级：一级不行时换谁",
    hint: "一级报错、超时、或者那个模型不支持图片时，自动换这一级再试一次。选了本地那一档要先看下面两项显存设置；显存不够会当场说明，不会硬加载。",
  },
  ollama_model: {
    kind: "line",
    label: "本地 Ollama 的模型名",
    hint: "例如 qwen3-vl:4b。只在「用本地 Ollama」时作数，也是对话模型看不了图时的退路。",
  },
  ollama: {
    kind: "line",
    label: "Ollama 地址",
    hint: "通常是 http://127.0.0.1:11434。只有 Ollama 装在别的机器上才需要改。",
  },
  vram_guard: {
    kind: "boolean",
    label: "本地识图前先看显存",
    hint: "开着的话，加载本地视觉模型之前先问一次还剩多少显存；不够就不加载，直接说明原因。8GB 以下的显卡建议一直开着。",
  },
  need_vram_mb: {
    kind: "number", min: 1000, max: 40000,
    label: "本地视觉模型要多少显存（MiB）",
    hint: "qwen3-vl:4b 大约要 4700-5200。换成更大的模型就往上调。",
  },
  vision_max_tokens: {
    kind: "number", min: 256, max: 32000,
    label: "单次识图最多输出多少 token",
    hint: "思考型模型（比如 deepseek 那几档）会先想一大段再写正文，额度给小了正文就被截掉、只剩空回复。6000 够一般的拼图描述；换了更爱想的模型再往上调。",
  },
  vision_no_think: {
    kind: "boolean",
    label: "识图时不先想一大段",
    hint: "开着的话，识图这一请求会带上 thinking: disabled，思考型模型（deepseek 那几档）直接写描述，不先耗几千 token 想。模型不认这个参数时会被忽略，不会报错。",
  },
  vision_prompt: {
    kind: "text",
    label: "识图提示词（整段替换）",
    hint: "留空用内置那段：说明这是按时间拼起来的屏幕截图、红格子数字是坐标刻度、只说看得见的、不超过 250 字。想让它更留意某类东西（比如「重点说报错和按钮」）就写在这里。可用 {question} 代表本次调用带的问题；不写这个占位符，问题会自动附在末尾。",
  },
  vision_timeout_ms: {
    kind: "number", min: 5000, max: 600000,
    label: "识图超时（毫秒）",
    hint: "单次送图给视觉模型最多等多久。云端模型或显存小、模型大时要放宽。",
  },
  run_timeout_ms: {
    kind: "number", min: 10000, max: 600000,
    label: "整次抓屏超时（毫秒）",
    hint: "从开始抓到出图的总体上限。抓的秒数调长了就得跟着放大，否则会被中途打断。",
  },
} as const;

type FieldKey = keyof typeof FIELDS;

const esc = (s: unknown) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);

/** 读 POST 请求体，限 64KB。 */
export async function readBody(req: any): Promise<any> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 64 * 1024) throw new Error("提交的内容太大了");
    chunks.push(Buffer.from(chunk));
  }
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    throw new Error("提交的内容不是合法 JSON");
  }
}

/** 只留下白名单里的字段，数字做钳制，布尔转真布尔。 */
export function sanitize(input: any): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!input || typeof input !== "object") return out;
  for (const [key, spec] of Object.entries(FIELDS)) {
    if (!(key in input)) continue;
    const value = (input as any)[key];
    if (spec.kind === "number") {
      const n = Number(value);
      if (!Number.isFinite(n)) continue;
      out[key] = Math.max(spec.min, Math.min(spec.max, Math.round(n)));
    } else if (spec.kind === "boolean") {
      out[key] = value === true || value === "true" || value === "on" || value === 1;
    } else if (spec.kind === "select") {
      const text = String(value ?? "").trim();
      // 动态下拉的选项来自运行时，这里只把长度挡住，真伪由插件在调用时再校验
      if ((spec as any).dynamic) {
        out[key] = text.slice(0, 200);
        continue;
      }
      const list = (spec as any).options as readonly any[] | undefined;
      const ok = Array.isArray(list) && list.some((o) => (typeof o === "string" ? o : o.value) === text);
      if (ok) out[key] = text;
    } else if (spec.kind === "line") {
      out[key] = String(value ?? "").trim().slice(0, 500);
    } else if (spec.kind === "text") {
      out[key] = String(value ?? "").slice(0, 4000);
    } else {
      out[key] = String(value ?? "").slice(0, 500);
    }
  }
  return out;
}

type GroupSpec = { id: string; title: string; desc: string; keys: string[] };

/** 设置页分组：一页一类。顺序就是页签顺序。 */
const GROUPS: GroupSpec[] = [
  {
    id: "env",
    title: "运行环境",
    desc: "先解决能不能跑。Python 解释器找对了，后面几页才有意义。改完点右上角的「测试配置」，它会真的起一次边车，报错会写在这里。",
    keys: ["python", "run_timeout_ms"],
  },
  {
    id: "capture",
    title: "抓屏",
    desc: "一次看屏抓多久、抓几帧、看整屏还是只看一块。这三样决定你看到的是「多久之内发生了什么」和「多大范围里的事」。默认值够日常用，视频、动效多的场景才往上调。",
    keys: ["seconds", "max_frames", "max_width", "focus", "target", "app", "fallback_on_miss", "tool_permission", "text_layer"],
  },
  {
    id: "sharp",
    title: "清晰度与坐标",
    desc: "帧怎么排、每一格多清楚；以及那条红刻度——刻度上的数字是屏幕像素坐标，可以直接照着说位置，也可以拿去裁。画面变化快时会自动缩小格子、塞更多帧，这页可以强制它偏一侧。",
    keys: ["tier_style", "tier_lock", "diff_level", "cut_abs", "need", "grid", "crop_min_side_pct", "crop_max_h", "monitors"],
  },
  {
    id: "disk",
    title: "存图与缓存池",
    desc: "屏幕内容要不要留在磁盘上、留多久、留多少。默认尽量短：成品图三天、原始帧阅后即焚。要事后单看某一帧，才把缓存池的秒数调大。",
    keys: ["shot_ttl_seconds", "pool_ttl", "pool_max_files", "pool_max_mb"],
  },
  {
    id: "vision",
    title: "识图模型",
    desc: "图片交给谁看。一级不行会自动换二级，两级都不行才是报错。最后四项只管本地 Ollama 那一档——不走本地就一直是灰的，不用管。",
    keys: ["vision_primary", "vision_backup", "vision_max_tokens", "vision_no_think", "vision_prompt", "vision_timeout_ms", "ollama_model", "ollama", "vram_guard", "need_vram_mb"],
  },
];

function pairUp(keys: string[], html: Map<string, string>): string {
  const out: string[] = [];
  let buf: string[] = [];
  const flush = () => {
    if (!buf.length) return;
    out.push(`<div class="pg-pair">${buf.join("")}</div>`);
    buf = [];
  };
  for (const key of keys) {
    const cell = html.get(key) ?? "";
    // 多行文本自己占一整行，挤在半宽里没法写
    if ((FIELDS as any)[key]?.kind === "text") {
      flush();
      out.push(`<div class="pg-pair"><div class="pg-item pg-wide">${cell}</div></div>`);
      continue;
    }
    buf.push(cell);
    if (buf.length === 2) flush();
  }
  flush();
  return out.join("\n");
}

type ChatModelOption = { id: string; name: string; model: string };

function field(key: FieldKey, cfg: any, chatModels: ChatModelOption[]): string {
  const spec: any = FIELDS[key];
  const value = cfg?.[key];
  const name = esc(key);
  const label = esc(spec.label);
  const hint = spec.hint ? `<p class="pg-hint">${esc(spec.hint)}</p>` : "";
  let control: string;
  if (spec.kind === "boolean") {
    control = `<label class="pg-row pg-check"><input type="checkbox" name="${name}"${value ? " checked" : ""}><span class="pg-label">${label}</span></label>`;
  } else if (spec.kind === "select") {
    let options: Array<{ value: string; label: string }> = spec.options as any;
    if (spec.dynamic === "chat") {
      const lead = (spec.lead as Array<{ value: string; label: string }>) ?? [{ value: "", label: "跟随当前会话模型" }];
      const tail = (spec.tail as Array<{ value: string; label: string }>) ?? [];
      options = lead.concat(
        chatModels.map((m) => ({ value: m.id, label: `${m.name}（${m.model}）` })),
        tail,
      );
      const want = String(value ?? "").trim();
      const reserved = lead.concat(tail).map((o) => o.value);
      if (want && !reserved.includes(want) && !chatModels.some((m) => m.id === want)) {
        options.push({ value: want, label: `${want}（当前配置里没有这个名字）` });
      }
    }
    const opts = options
      .map((o) => {
        const selected = String(value ?? "") === o.value;
        return `<option value="${esc(o.value)}"${selected ? " selected" : ""}>${esc(o.label)}</option>`;
      })
      .join("");
    control = `<label class="pg-row"><span class="pg-label">${label}</span><select name="${name}">${opts}</select></label>`;
  } else if (spec.kind === "number") {
    control = `<label class="pg-row"><span class="pg-label">${label}</span><input type="number" name="${name}" value="${esc(value)}" min="${spec.min}" max="${spec.max}" step="${spec.step ?? 1}"></label>`;
  } else if (spec.kind === "line") {
    control = `<label class="pg-row"><span class="pg-label">${label}</span><input type="text" name="${name}" value="${esc(value)}"></label>`;
  } else if (spec.kind === "text") {
    control = `<label class="pg-row"><span class="pg-label">${label}</span><textarea name="${name}" rows="7">${esc(value)}</textarea></label>`;
  } else {
    control = `<label class="pg-row"><span class="pg-label">${label}</span><input type="text" name="${name}" value="${esc(value)}"></label>`;
  }
  const dep = (spec as any).dep as { key: string; values: string[] } | undefined;
  const depAttr = dep ? ` data-dep="${esc(dep.key)}" data-dep-values="${esc(dep.values.join(","))}"` : "";
  return `<div class="pg-item"${depAttr}>${control}${hint}</div>`;
}

/** 渲染配置页：结构和配色都走 /ui/styles.css，跟管理页同一套。
 * chatModels 是「用哪个对话模型看图」下拉的选项，由 index.ts 从宿主那边取来传进来。
 */
export function renderPage(cfg: any, chatModels: ChatModelOption[] = [], warnings: string[] = []): string {
  const warnBlock = warnings.length
    ? `<section class="panel" style="border-color:rgba(220,38,38,.45)"><div class="panel-header"><h2 style="color:#b91c1c">注意</h2></div><div class="panel-body">${warnings
        .map((w) => `<p class="pg-hint" style="margin:0 0 8px;color:#b91c1c">${esc(w)}</p>`)
        .join("")}</div></section>\n`
    : "";
  const html = new Map((Object.keys(FIELDS) as FieldKey[]).map((k) => [k, field(k, cfg, chatModels)] as const));
  const tabsHtml = GROUPS.map((g, i) =>
    `<button type="button" class="pg-tab${i === 0 ? " on" : ""}" data-goto="${esc(g.id)}">${esc(g.title)}</button>`,
  ).join("");
  const groupsHtml = GROUPS.map((g, i) =>
    `<section class="pg-group" id="pg-${esc(g.id)}"${i === 0 ? "" : " hidden"}>` +
      `<p class="pg-desc">${esc(g.desc)}</p>` +
      pairUp(g.keys, html) +
      `</section>`,
  ).join("\n");
  return `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>看屏 · 配置</title>
<link rel="stylesheet" href="/ui/styles.css">
<style>
.panel-body .pg-row{display:block;margin:0}
.panel-body .pg-label{display:block!important;font-size:15px!important;font-weight:700!important;color:inherit;margin:0 0 10px;line-height:1.45}
.panel-body .pg-row input[type=text],.panel-body .pg-row input[type=number],.panel-body .pg-row select{display:block;width:100%;box-sizing:border-box;padding:10px 13px;font-size:14px;line-height:1.4;border-radius:8px;border:1px solid rgba(127,127,127,.35);background:transparent;color:inherit;font-family:inherit}
.panel-body .pg-row input:focus,.panel-body .pg-row select:focus{outline:none;border-color:#7c5cff;box-shadow:0 0 0 3px rgba(124,92,255,.12)}
.panel-body .pg-check{display:flex!important;align-items:center;gap:10px;padding-top:2px}
.panel-body .pg-check .pg-label{margin:0}
.panel-body .pg-pair{display:grid;grid-template-columns:1fr 1fr;column-gap:36px;margin:0 0 30px}
.panel-body .pg-pair:last-child{margin-bottom:0}
.panel-body .pg-pair>.pg-item:only-child{grid-column:1/-1}
.panel-body .pg-item{min-width:0}
.panel-body .pg-item.pg-off{opacity:.45}
.panel-body .pg-item.pg-off .pg-hint{opacity:.8}
.panel-body .pg-hint{margin:10px 0 0;font-size:12px;line-height:1.65;color:#797e8c}
.panel-body .pg-ro{display:grid;grid-template-columns:150px 1fr;gap:16px;margin:14px 0;font-size:13px;align-items:baseline}
.panel-body .pg-ro b{font-weight:600;color:#797e8c}
.panel-body .pg-ro code{background:rgba(127,127,127,.12);padding:2px 6px;border-radius:5px;word-break:break-all;font-size:12px}
.panel-body .pg-row textarea{display:block;width:100%;box-sizing:border-box;padding:11px 13px;border:1px solid rgba(127,127,127,.28);border-radius:8px;font:inherit;font-size:14px;line-height:1.7;min-height:130px;resize:vertical;background:transparent;color:inherit}
.panel-body .pg-row textarea:focus{outline:none;border-color:#7c5cff;box-shadow:0 0 0 3px rgba(124,92,255,.12)}
.panel-body .pg-wide{grid-column:1/-1}
.pg-tabs{display:flex;flex-wrap:wrap;gap:6px;padding:0;margin:0;border-bottom:0}
.pg-tabs+.pg-actions{margin-left:auto}
.pg-tab{background:transparent;border:1px solid transparent;border-radius:8px;padding:7px 13px;font-size:14px;color:#797e8c;cursor:pointer;font-family:inherit;line-height:1.2}
.pg-tab:hover{background:rgba(124,92,255,.08);color:#6d4aff}
.pg-tab.on{background:rgba(124,92,255,.12);border-color:rgba(124,92,255,.35);color:#6d4aff;font-weight:600}
.pg-desc{margin:0 0 22px;padding:0 0 14px;font-size:13px;line-height:1.75;color:#797e8c;border-bottom:1px dashed rgba(127,127,127,.22)}
.pg-actions{display:flex;align-items:center;gap:12px;margin-left:auto}
#msg{font-size:13px;color:#059669;white-space:pre-line}
#msg.err{color:#dc2626}
.pg-result{margin:22px 0 0;padding:14px 16px;border-radius:10px;font-size:14px;line-height:1.7;white-space:pre-wrap;word-break:break-word;border:1px solid transparent}
.pg-result.wait{background:rgba(124,92,255,.10);border-color:rgba(124,92,255,.35);color:#6d4aff}
.pg-result.ok{background:rgba(5,150,105,.10);border-color:rgba(5,150,105,.35);color:#047857}
.pg-result.err{background:rgba(220,38,38,.10);border-color:rgba(220,38,38,.35);color:#b91c1c}
#msg{display:none}
@media(max-width:760px){.panel-body .pg-pair{grid-template-columns:1fr}}
</style></head><body>
<form id="f">
${warnBlock}<section class="panel"><div class="panel-header"><div class="pg-tabs">${tabsHtml}</div><div class="pg-actions"><button class="btn" type="button" id="test">测试配置</button><button class="btn primary" type="submit" form="f" id="ok">保存</button><span id="msg"></span></div></div><div class="panel-body">
${groupsHtml}
<div id="result" class="pg-result" hidden></div>
</div></section>
<section class="panel"><div class="panel-header"><h2>只读</h2><span class="badge">改不了</span></div><div class="panel-body">
<div class="pg-ro"><b>边车脚本</b><code>${esc(cfg?.script ?? "")}</code></div>
<div class="pg-ro"><b>出图目录</b><code>${esc(cfg?.shot_dir ?? "")}</code></div>
<p class="pg-hint" style="margin-left:0">这两项由宿主按插件目录算，写死容易弄坏，不在表单里。</p>
</div></section>
</form>
<script type="module">
const form = document.getElementById("f");
const msg = document.getElementById("msg");
const ok = document.getElementById("ok");
const depBoxes = [...form.querySelectorAll("[data-dep]")];
const syncDeps = () => {
  for (const box of depBoxes) {
    const src = form.elements[box.getAttribute("data-dep")];
    const vals = (box.getAttribute("data-dep-values") || "").split(",");
    const on = src ? vals.includes(src.value) : true;
    for (const el of box.querySelectorAll("input,select,textarea")) el.disabled = !on;
    box.classList.toggle("pg-off", !on);
  }
};
const tabs = [...form.querySelectorAll(".pg-tab")];
const groups = [...form.querySelectorAll(".pg-group")];
const goto = (id) => {
  for (const t of tabs) t.classList.toggle("on", t.getAttribute("data-goto") === id);
  for (const g of groups) g.hidden = g.id !== "pg-" + id;
};
for (const t of tabs) t.addEventListener("click", () => goto(t.getAttribute("data-goto")));
form.addEventListener("change", syncDeps);
syncDeps();
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    data[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  ok.disabled = true; msg.className = ""; msg.textContent = "保存中…";
  try {
    const res = await fetch("/plugins/peek/save", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || ("HTTP " + res.status));
    msg.textContent = "已保存，下次调用生效";
  } catch (error) {
    msg.className = "err";
    msg.textContent = "没存上：" + (error?.message || error);
  } finally { ok.disabled = false; }
});
const result = document.getElementById("result");
const show = (kind, text) => {
  result.hidden = false;
  result.className = "pg-result " + kind;
  result.textContent = text;
};
document.getElementById("test").addEventListener("click", async () => {
  const btn = document.getElementById("test");
  btn.disabled = true;
  show("wait", "测试中…请稍等");
  try {
    const res = await fetch("/plugins/peek/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.ok === false) throw new Error(body.error || ("HTTP " + res.status));
    show("ok", Array.isArray(body.lines) ? body.lines.join(String.fromCharCode(10)) : (body.detail || body.text || "正常"));
  } catch (error) {
    show("err", "测试失败：" + (error?.message || error));
  } finally { btn.disabled = false; }
});
</script></body></html>`;
}
