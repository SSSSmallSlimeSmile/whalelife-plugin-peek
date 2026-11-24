/*! WhaleLife「看屏」插件：按变化取帧。
 *
 * 抓屏、判分、留帧、装箱、认字全在 Python 边车里（沿用上游那份 941 行主线，
 * 只把 AstrBot 插件壳换成了命令行）。这边只管三件事：
 *   拉起边车、读回 JSON、把拼好的图交给识图模型说一遍。
 *
 * 识图分两级：一级默认交给对话模型（跟随当前会话，或指定某一个），
 * 二级默认走本地 Ollama。一级报错、超时或模型不支持图片时，自动换二级再试一次。
 *
 * 与 eyes 的区别：eyes 只看当前一帧；这里抓一段，只为「画面变了」的帧付 token。
 */
import type { Context } from "../../core/context.ts";
import { execFile, spawnSync } from "node:child_process";
import { readFile, mkdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readBody, renderPage, sanitize } from "./ui.ts";

const HERE = dirname(fileURLToPath(import.meta.url));

const DEFAULTS = {
  /** 边车用哪个 Python。留空则按「配置 → WHALELIFE_PEEK_PYTHON → PATH 里的 python」找。 */
  python: "",
  script: join(HERE, "see_now_sidecar.py"),
  shot_dir: join(HERE, "shots"),
  /** 抓多久，秒。 */
  seconds: 10,
  /** 一张图最多装几帧。 */
  max_frames: 6,
  /** native / up / down —— 档位整体偏移。 */
  tier_style: "native",
  /** 1-3 锁死一档，0 不锁。 */
  tier_lock: 0,
  /** whole / content / auto —— auto 先整屏抓，热点占屏 8%-60% 才裁。 */
  focus: "auto",
  /** 成品图与单帧裁切图的最大宽度，400-4096。 */
  max_width: 1600,
  /** 坐标网格：0 关，auto 自动步长，或直接给像素步长。只叠在单帧上。 */
  grid: "auto",
  /** 抓哪些显示器：primary 主屏 / all 全部 / 编号逗号分隔（如 1,3）。 */
  monitors: "primary",
  /** 单帧裁切的形状判据：变化区宽或高窄于全屏的这个百分比就不裁（10 = 一成）。 */
  crop_min_side_pct: 10,
  /** 裁剪图的兜底高度上限，超出就等比缩小。宽度同时封顶 800。 */
  crop_max_h: 1400,
  /** 截图在磁盘上留几秒。0 = 只留本次这张，下次抓屏时清掉，屏幕内容不长期落地。 */
  shot_ttl_seconds: 0,
  /** 帧缓存池：留下的帧逐张存一份，供之后单独看某一帧。0 = 阅后即焚，不落盘。 */
  pool_ttl: 0,
  /** 缓存池目录，留空则 shots/pool。 */
  pool_dir: "",
  /** 池里最多留几张，超了从最旧的删。 */
  pool_max_files: 200,
  /** 池里最多占多少 MB，超了从最旧的删。 */
  pool_max_mb: 300,
  /** UIA + OCR 认一次字，慢，默认关。 */
  text_layer: false,
  /** 一级识图：chat = 跟随当前会话的模型；也可以直接填某个对话模型的 id；ollama = 本地。 */
  vision_primary: "chat",
  /** 二级备用：一级报错、超时或不支持图片时换它。none = 不备用。 */
  vision_backup: "ollama",
  /** 本地 Ollama 里的视觉模型名，也是对话模型看不了图时的退路。 */
  ollama_model: "qwen3-vl:4b",
  ollama: "http://127.0.0.1:11434",
  /** 本地识图之前先看显存够不够：小显卡上 4B 模型能把整张卡挤爆。 */
  vram_guard: true,
  /** 加载本地视觉模型至少要多少空闲显存（MiB）。 */
  need_vram_mb: 5200,
  /** 单次识图最多输出多少 token。思考型模型会先想一段，太小会把正文挤掉。 */
  /** 识图时让思考型模型别先想一大段：只想不写，正文就会被输出上限挤掉。 */
  vision_no_think: true,
  /** 识图提示词。留空用内置那段；支持 {question} 占位，不写就自动附在末尾。 */
  vision_prompt: "",
  vision_max_tokens: 6000,
  vision_timeout_ms: 120_000,
  /** 边车总超时：抓多久 + 装箱 + 认字，留足余量。 */
  run_timeout_ms: 180_000,
};

type Config = typeof DEFAULTS;

type SidecarResult = {
  status?: string;
  message?: string;
  path?: string;
  shot_frames?: number;
  kept_frames?: number;
  seconds?: number;
  elapsed_ms?: number;
  pool?: Array<{ i?: number; t?: number; file?: string; score?: number }>;
  reused?: boolean;
  look?: string;
  note?: string;
  tier?: string;
  static?: boolean;
  grid?: { step?: number; size?: number[] };
  monitors?: {
    picked?: number[];
    count?: number;
    origin?: number[];
    all?: Array<{ n?: number; primary?: boolean; rect?: number[] }>;
  };
  text?: {
    ok?: boolean;
    window?: string;
    text?: string;
    blocks?: Array<{ k?: string; t?: string; r?: string; x?: number; y?: number }>;
    element_count?: number;
    line_count?: number;
    ocr_lang?: string;
  };
};

/** 找 Python：配置里的路径 → 环境变量 WHALELIFE_PEEK_PYTHON → PATH 里的 python。 */
function resolvePython(cfgPath: string): string {
  const want = String(cfgPath ?? "").trim();
  if (want && existsSync(want)) return want;
  if (want) console.warn(`[peek] 配置的 python 不存在：${want}，继续往下找`);
  const envPath = String(process.env.WHALELIFE_PEEK_PYTHON ?? "").trim();
  if (envPath && existsSync(envPath)) return envPath;
  if (envPath) console.warn(`[peek] WHALELIFE_PEEK_PYTHON 指向的文件不存在：${envPath}`);
  return "python";
}

/** 跑一次边车，把 stdout 的那一行 JSON 解出来。 */
function runSidecar(cfg: Config, seconds: number, maxFrames: number, maxWidth: number): Promise<SidecarResult> {
  const args = [
    cfg.script,
    "--seconds", String(seconds),
    "--max-frames", String(maxFrames),
    "--tier-style", String(cfg.tier_style),
    "--tier-lock", String(cfg.tier_lock),
    "--focus", String(cfg.focus),
    "--max-width", String(maxWidth),
    "--shot-dir", String(cfg.shot_dir),
  ];
  if (cfg.text_layer) args.push("--text-layer");
  args.push("--grid", String(cfg.grid ?? "0"));
  args.push("--monitors", String(cfg.monitors ?? "primary"));
  args.push("--keep-seconds", String(cfg.shot_ttl_seconds ?? 0));  args.push("--pool-dir", String(cfg.pool_dir ?? ""));
  args.push("--pool-ttl", String(cfg.pool_ttl ?? 0));
  args.push("--pool-max-files", String(cfg.pool_max_files ?? 200));
  args.push("--pool-max-mb", String(cfg.pool_max_mb ?? 300));
  args.push("--crop-min-side-pct", String(cfg.crop_min_side_pct ?? 10));
  args.push("--crop-max-h", String(cfg.crop_max_h ?? 1400));


  return new Promise((ok, fail) => {
    execFile(
      resolvePython(cfg.python),
      args,
      {
        cwd: HERE,
        timeout: cfg.run_timeout_ms,
        windowsHide: true,
        maxBuffer: 8 * 1024 * 1024,
        env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
      },
      (err, stdout, stderr) => {
        if ((err as any)?.code === "ENOENT") {
          const py = resolvePython(cfg.python);
          fail(
            new Error(
              `没找到 Python 解释器（${py}）。装一个 Python 3，或在插件设置里填解释器的完整路径（data/config.json 的 python 字段），然后点「测试配置」。`,
            ),
          );
          return;
        }
        const out = String(stdout ?? "").trim();
        const line = out.split(/\r?\n/).filter((s) => s.trim().startsWith("{")).pop();
        if (line) {
          try {
            const data = JSON.parse(line) as SidecarResult;
            if (data.status === "error") {
              fail(new Error(data.message || "边车报错（没给原因）"));
              return;
            }
            ok(data);
            return;
          } catch (error: any) {
            fail(new Error(`边车输出不是 JSON：${error?.message ?? error}`));
            return;
          }
        }
        const tail = String(stderr ?? "").trim().split(/\r?\n/).slice(-3).join(" / ");
        fail(
          new Error(
            err
              ? `边车没跑成（${(err as any)?.code ?? err?.message}）${tail ? "：" + tail : ""}`
              : "边车没有任何输出",
          ),
        );
      },
    );
  });
}

const BUILTIN_PROMPT =
  "这几张图是从屏幕上抓的，可能一张也可能几张，按时间顺序拼在一起（格子越大说明那一刻画面变化越大）。" +
    "图上若有红色格子和边上的数字，那是坐标刻度，标的是这块屏幕的像素位置——它不是画面里的内容，也不是时间轴或界面元素；只在要指出某个东西在哪儿时才引用它。" +
    "直接说结论：屏幕上在干什么、打开了什么窗口、有什么文字或报错。" +
  "只写图上能看出来的。看不清、被遮住、或者拿不准的，就写「看不清」，不要补全，不要替界面里没出现的按钮和文字编说法。不超过 250 字。";

/** 这次送图的提示词：配置里写了就用配置，{question} 会被替换成本次的问题。 */
function buildPrompt(cfg: Config, question: string): string {
  const custom = String(cfg.vision_prompt ?? "").trim();
  const asked = question ? `另外回答这个问题：${question}` : "";
  if (!custom) return BUILTIN_PROMPT + (asked ? "\n" + asked : "");
  if (custom.includes("{question}")) return custom.split("{question}").join(question || "（这次没有额外问题）");
  return asked ? custom + "\n" + asked : custom;
}
/** 可用作识图的对话模型：宿主登记过、且内核能直接调用。 */
function chatModels(ctx: Context): Array<{ id: string; name: string; model: string }> {
  try {
    return ctx.models
      .list("chat")
      .filter((m) => m.callable !== false)
      .map((m) => ({ id: m.id, name: m.name, model: m.model }));
  } catch {
    return [];
  }
}

/** 这次识图用哪个对话模型：指定的那个 → 当前会话人格的模型 → 列表里第一个能用的。 */
function pickChatModel(
  ctx: Context,
  session: any,
  want: string | null,
): { id: string; label: string } | null {
  const all = chatModels(ctx);
  const wanted = String(want ?? "").trim();
  if (wanted) {
    const hit =
      all.find((m) => m.id === wanted) ?? all.find((m) => m.model === wanted) ?? all.find((m) => m.name === wanted);
    if (hit) return { id: hit.id, label: `${hit.name}（${hit.model}）` };
    console.warn(`[peek] 配置的对话模型 ${wanted} 不在可用列表里，改用当前会话的模型`);
  }
  const personaId = String(session?.personaId ?? "").trim();
  if (personaId) {
    try {
      const persona = ctx.personas.list().find((p) => p.id === personaId);
      const bound = String(persona?.model ?? "").trim();
      if (bound) {
        const hit = all.find((m) => m.id === bound);
        if (hit) return { id: hit.id, label: `${hit.name}（${hit.model}）` };
      }
    } catch {
      // 拿不到人格就当没指定，往下走
    }
  }
  const first = all[0];
  return first ? { id: first.id, label: `${first.name}（${first.model}）` } : null;
}

/** 配置里那一档写了什么：none/off/空 = 不用；ollama = 本地；chat = 跟随会话；其它当模型 id。 */
function parseTarget(raw: unknown): { kind: "chat"; id: string | null } | { kind: "ollama" } | null {
  const v = String(raw ?? "").trim();
  const low = v.toLowerCase();
  if (!v || low === "none" || low === "off" || low === "no") return null;
  if (low === "ollama") return { kind: "ollama" };
  if (low === "chat") return { kind: "chat", id: null };
  return { kind: "chat", id: v };
}

/** 识图要依次试的几档：一号不行才用二号。两档碰巧是同一个模型就只留一份。 */
export function buildChain(
  ctx: Context,
  cfg: Config,
  session: any,
): Array<{ kind: "chat" | "ollama"; id: string; label: string }> {
  const chain: Array<{ kind: "chat" | "ollama"; id: string; label: string }> = [];
  const tiers: Array<[string, unknown]> = [
    ["一级", cfg.vision_primary],
    ["二级", cfg.vision_backup],
  ];
  for (const [tier, raw] of tiers) {
    const target = parseTarget(raw);
    if (!target) continue;
    if (target.kind === "ollama") {
      if (chain.some((c) => c.kind === "ollama")) continue;
      chain.push({ kind: "ollama", id: cfg.ollama_model, label: `${cfg.ollama_model}（本地）` });
      continue;
    }
    const pick = pickChatModel(ctx, session, target.id);
    if (!pick) {
      ctx.logger.warn(`看屏识图：${tier}「${String(raw)}」找不到可用的对话模型，跳过`);
      continue;
    }
    if (chain.some((c) => c.id === pick.id)) continue;
    chain.push({ kind: "chat", id: pick.id, label: pick.label });
  }
  return chain;
}

/** 交给对话模型看图。 */
async function describeChat(
  ctx: Context,
  modelId: string,
  cfg: Config,
  file: string,
  question: string,
): Promise<string> {
  const b64 = (await readFile(file)).toString("base64");
  const ask = (noThink: boolean) => {
    // parameters 不在 ChatOptions 的类型里，但宿主会把它原样透传给服务商；
    // 有的 openai 兼容端点（Gemini）见 "thinking" 就整个 400，所以按需带、按需去。
    const opts: any = {
      model: modelId,
      messages: [
        {
          role: "user",
          content: [
            { type: "text", text: buildPrompt(cfg, question) },
            { type: "image_url", image_url: { url: `data:image/png;base64,${b64}` } },
          ],
        },
      ],
      max_tokens: cfg.vision_max_tokens,
      signal: AbortSignal.timeout(cfg.vision_timeout_ms),
    };
    if (noThink) opts.parameters = { thinking: { type: "disabled" } };
    return ctx.llm.chat(opts);
  };
  // 有的服务商的 openai 兼容端点不认 thinking 字段，见到就整个请求 400。
  // 只在这一种情况下退一步重试，别的错误不吞。
  let reply: any;
  try {
    reply = await ask(cfg.vision_no_think);
  } catch (error: any) {
    const msg = String(error?.message ?? error);
    if (cfg.vision_no_think && /HTTP 400/.test(msg)) {
      ctx.logger.warn(`识图模型 ${modelId} 不认 thinking 参数，去掉后重试`);
      reply = await ask(false);
    } else {
      throw error;
    }
  }
  const text = String(reply?.text ?? "").trim();
  if (!text) {
    const pt = Number(reply?.usage?.prompt_tokens ?? 0);
    const why =
      reply?.finish_reason === "length"
        ? "正文还没开始就被输出上限截断（思考把额度吃完了）"
        : pt > 0 && pt < 1000
          ? `图片多半没送进去（这次提示只有 ${pt} token）`
          : reply?.reasoning_content
            ? "只返回了思考内容，没有正文"
            : "返回了空内容，多半是这个模型不支持图片输入";
    throw new Error(`对话模型没有返回描述：${why}`);
  }
  return text;
}

/** 问一次 nvidia-smi：现在还剩多少空闲显存（MiB）。查不到返回 null。 */
function freeVramMb(): number | null {
  const r = spawnSync("nvidia-smi", ["--query-gpu=memory.free", "--format=csv,noheader,nounits"], {
    encoding: "utf8",
    timeout: 5000,
    windowsHide: true,
  });
  if (r.error || r.status !== 0) return null;
  const n = parseInt(String(r.stdout ?? "").trim().split(/\s+/)[0], 10);
  return Number.isFinite(n) ? n : null;
}

/** 本地那条路能不能走。不够就别硬加载，把话说清楚交给调用方。 */
function vramShort(cfg: Config): string | null {
  if (!cfg.vram_guard) return null;
  const free = freeVramMb();
  // 查不到就不拦——没装 nvidia-smi 的机器不该被这一条误伤。
  if (free === null) return null;
  if (free >= cfg.need_vram_mb) return null;
  return (
    `本地显存不够：只剩 ${free} MiB，加载 ${cfg.ollama_model} 至少要 ${cfg.need_vram_mb} MiB。` +
    `关掉别的东西再试，或者把识图改成走对话模型。`
  );
}

/** 交给本地 Ollama 看图。 */
async function describeOllama(cfg: Config, model: string, file: string, question: string): Promise<string> {
  // 放在 try 外面：显存不够不是「连不上」，别被下面的兜底包成连接错误。
  const short = vramShort(cfg);
  if (short) throw new Error(short);
  const b64 = (await readFile(file)).toString("base64");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), cfg.vision_timeout_ms);
  try {
    const res = await fetch(`${cfg.ollama}/api/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model,
        prompt: buildPrompt(cfg, question),
        images: [b64],
        stream: false,
        options: { temperature: 0.1 },
      }),
      signal: controller.signal,
    });
    if (!res.ok) throw new Error(`本地 Ollama 返回 HTTP ${res.status}`);
    const data: any = await res.json();
    const text = String(data?.response ?? "").trim();
    if (!text) throw new Error("本地 Ollama 没有返回描述");
    return text;
  } catch (error: any) {
    if (controller.signal.aborted) throw new Error("本地 Ollama 响应超时");
    if (String(error?.message ?? "").startsWith("本地 Ollama")) throw error;
    throw new Error(`连不上本地 Ollama（${cfg.ollama}）：${error?.message ?? error}`);
  } finally {
    clearTimeout(timer);
  }
}

/** 识图总入口：一级不行换二级，两级都不成才把两边的原因一起报出来。 */
async function describe(
  ctx: Context,
  cfg: Config,
  session: any,
  file: string,
  question: string,
): Promise<{ text: string; by: string; note?: string }> {
  const chain = buildChain(ctx, cfg, session);
  if (!chain.length) throw new Error("一级和二级都没落上，没有可用的识图模型");
  const tried: string[] = [];
  for (const leg of chain) {
    try {
      const text =
        leg.kind === "ollama"
          ? await describeOllama(cfg, cfg.ollama_model, file, question)
          : await describeChat(ctx, leg.id, cfg, file, question);
      if (!tried.length) return { text, by: leg.label };
      ctx.logger.info(`看屏识图：换到 ${leg.label} 才成`);
      return { text, by: leg.label, note: `${tried.join("；")}；换到${leg.kind === "ollama" ? "本地" : "备用"} ${leg.label} 成了` };
    } catch (error: any) {
      tried.push(`${leg.label} 没成：${String(error?.message ?? error)}`);
    }
  }
  throw new Error(tried.join("；"));
}

/** 旧配置迁移：以前是 vision_mode + vision_model，换成一级/二级。 */
function normalizeSaved(raw: any): any {
  if (!raw || typeof raw !== "object") return raw ?? {};
  const out: any = { ...raw };
  if (out.vision_primary === undefined) {
    const mode = String(out.vision_mode ?? "").trim().toLowerCase();
    const want = String(out.vision_model ?? "").trim();
    if (mode === "ollama") out.vision_primary = "ollama";
    else if (want) out.vision_primary = want;
    else out.vision_primary = "chat";
  }
  delete out.vision_mode;
  delete out.vision_model;
  return out;
}

/** plugins.json 的 disabled 里没有 eyes，就算它开着。读不到也按开着算，宁可多提醒一次。 */
async function eyesRunning(): Promise<boolean> {
  try {
    const raw = await readFile(join(HERE, "..", "..", "config", "plugins.json"), "utf8");
    const list = JSON.parse(raw)?.disabled;
    return !(Array.isArray(list) && list.includes("eyes"));
  } catch {
    return true;
  }
}

export async function apply(ctx: Context) {
  let saved: Partial<Config> | null = null;
  try {
    saved = (await ctx.config.read("config.json")) as Partial<Config> | null;
  } catch {
    saved = null;
  }
  let cfg: Config = { ...DEFAULTS, ...normalizeSaved(saved) };

  /** 每次调用重读配置：改 config.json 不用重启进程。 */
  const reload = async () => {
    try {
      const again = (await ctx.config.read("config.json")) as Partial<Config> | null;
      cfg = { ...DEFAULTS, ...normalizeSaved(again) };
    } catch {
      // 读不到就沿用上一次那份
    }
  };

  try {
    await mkdir(cfg.shot_dir, { recursive: true });
  } catch {
    // 建不出来也先别拦，边车那边会自己报
  }
  if (!existsSync(resolve(cfg.script))) {
    ctx.logger.warn(`看屏边车不在：${cfg.script}`);
  }
  if (await eyesRunning()) {
    ctx.logger.warn(
      "eyes 和 peek 同时启用：两者都要用本地视觉模型，显存不够会把整机挤到卡死。建议在插件管理里停用 eyes。",
    );
  }

  ctx.tools.register({
    name: "see_now",
    description:
      "抓一段屏幕（默认 10 秒），只保留画面真正变化的那几帧，拼成一张图后看图说话。" +
      "问「我在干嘛」「屏幕上是什么」「刚才弹出什么了」「这个界面有什么」时用它。" +
      "画面不动的时间几乎不留帧，所以不用担心浪费；但一次要等几秒到十几秒。",
    schema: {
      type: "object",
      properties: {
        seconds: { type: "number", description: "抓多久，默认 10 秒，范围 2-60" },
        max_frames: { type: "number", description: "一张图最多装几帧，默认 6，范围 1-12" },
        max_width: { type: "number", description: "成品图最大宽度，默认 1600，范围 400-4096" },
        grid: {
          type: "string",
          description: "坐标网格：auto（默认，自动步长）、0 关闭，或给像素步长。只对单帧生效，标的数是屏幕像素坐标",
        },
        pool_ttl_seconds: {
          type: "number",
          description: "这次留下的帧在缓存池里放多少秒，0 = 阅后即焚（默认跟随配置）。给了就覆盖这一次",
        },
        monitors: {
          type: "string",
          description: "抓哪几块屏：primary（默认，主屏）、all（全部）、或编号逗号分隔如 1,3。只在多显示器时有区别",
        },
        question: { type: "string", description: "想重点看什么，可省略" },
      },
      required: [],
    },
    danger: "safe",
    run: async (args: any, session: any) => {
      const seconds = Math.max(2, Math.min(60, Number(args?.seconds ?? cfg.seconds) || cfg.seconds));
      const maxFrames = Math.max(1, Math.min(12, Number(args?.max_frames ?? cfg.max_frames) || cfg.max_frames));
      const maxWidth = Math.max(400, Math.min(4096, Number(args?.max_width ?? cfg.max_width) || cfg.max_width));
      const question = String(args?.question ?? "").trim();
      // 本次调用的网格设置只影响这一次，不写回 cfg——下面 reload() 会把它盖掉，
      // 以前就是在 reload 之后生效不出效果。
      const gridArg =
        args?.grid !== undefined && args?.grid !== null && String(args.grid).trim() !== ""
          ? String(args.grid).trim()
          : String(cfg.grid ?? DEFAULTS.grid);
      const poolArg =
        args?.pool_ttl_seconds !== undefined && args?.pool_ttl_seconds !== null && Number.isFinite(Number(args.pool_ttl_seconds))
          ? Math.max(0, Number(args.pool_ttl_seconds))
          : Number(cfg.pool_ttl ?? 0);
      const monitorsArg =
        args?.monitors !== undefined && args?.monitors !== null && String(args.monitors).trim() !== ""
          ? String(args.monitors).trim()
          : String(cfg.monitors ?? DEFAULTS.monitors);
      await reload();

      let data: SidecarResult;
      try {
        data = await runSidecar(
          { ...cfg, grid: gridArg, pool_ttl: poolArg, monitors: monitorsArg },
          seconds, maxFrames, maxWidth);
      } catch (error: any) {
        return `错误：${error?.message ?? String(error)}`;
      }

      const file = String(data.path ?? "");
      const mon = data.monitors;
      const monLine =
        mon && (mon.count ?? 0) > 1
          ? `屏：第 ${(mon.picked ?? []).join("、")} 块（共 ${mon.count} 块）`
          : "";
      const head = [
        `抓 ${data.shot_frames ?? 0} 帧，留 ${data.kept_frames ?? 0} 帧，${data.seconds ?? seconds} 秒`,
        data.tier ? `档位：${data.tier}` : "",
        monLine,
      ]
        .filter(Boolean)
        .join("｜");

      // 屏幕几乎没动：边车把上一张原样递回来了，不重复识图。
      if (data.reused) {
        const reuseLook = String(data.look ?? "").trim();
        return [
          head,
          `图：${file}（复用上次那张）`,
          reuseLook ? `上次的描述：${reuseLook}` : "",
          data.note ?? "",
        ]
          .filter(Boolean)
          .join("\n");
      }

      const parts = [head, `图：${file}`];
      // 缓存池：这次留下的帧逐张存了一份，要抠细节就单独看某一帧。
      const poolList = Array.isArray(data.pool) ? data.pool : [];
      if (poolList.length) {
        const shown = poolList.slice(0, 6).map((f) => `第 ${f.i ?? "?"} 帧 ≈${f.t ?? 0}s ${f.file ?? ""}`);
        parts.push(
          `缓存池 ${poolList.length} 张（${poolArg > 0 ? `留 ${poolArg} 秒` : "阅后即焚"}）：\n` +
            shown.join("\n") +
            (poolList.length > 6 ? `\n…还有 ${poolList.length - 6} 张` : ""),
        );
      }

      const window = String(data.text?.window ?? "").trim();
      const blocks = data.text?.blocks ?? [];
      if (data.text?.ok && (window || blocks.length)) {
        const lines = blocks
          .slice(0, 40)
          .map((b) => (b.t ? String(b.t) : ""))
          .filter(Boolean);
        parts.push(
          `文字层（UIA + OCR${data.text.ocr_lang ? "，" + data.text.ocr_lang : ""}）` +
            (window ? `｜窗口：${window}` : "") +
            (lines.length ? "\n" + lines.join(" / ") : ""),
        );
      }

      if (!file || !existsSync(file)) {
        parts.push("没有出图，这部分没看成。");
        return parts.join("\n");
      }

      try {
        const look = await describe(ctx, cfg, session, file, question);
        parts.push(`屏幕描述（${look.by}）：\n${look.text}`);
        if (look.note) parts.push(`说明：${look.note}`);
      } catch (error: any) {
        parts.push(`图出来了但没识成：${error?.message ?? String(error)}`);
      }
      if (data.note) parts.push(`边车备注：${data.note}`);
      return parts.join("\n");
    },
  });

  ctx.web.api("/plugins/peek/test", async (req: any, res: any) => {
    const reply = (code: number, body: any) => {
      res.writeHead(code, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
      res.end(JSON.stringify(body));
    };
    const lines: string[] = [];
    let ok = true;
    try {
      await reload();

      // 1. python 起不起得来
      const py = resolvePython(cfg.python);
      const wantPy = String(cfg.python ?? "").trim();
      if (wantPy && !existsSync(wantPy))
        lines.push(`[!] 配置的 python 路径不存在：${wantPy}，已回落到 PATH 里的 python`);
      let pyOk = false;
      try {
        const out = spawnSync(py, ["-c", "import sys; print(sys.version.split()[0])"], { timeout: 15000, encoding: "utf8" });
        if (out.error || out.status !== 0) {
          ok = false;
          lines.push(`[×] Python：${py} 调不动 —— ${out.error?.message ?? String(out.stderr ?? "").trim() ?? ("退出码 " + out.status)}`);
          // 后面那条依赖检查没有意义，python 都起不来
          lines.push("    装一个 Python 3，或在插件设置里填解释器的完整路径（python 字段）；Windows 商店那个 python 别名不算。");
        } else {
          pyOk = true;
          lines.push(`[✓] Python ${String(out.stdout ?? "").trim()}：${py}`);
        }
      } catch (error: any) {
        ok = false;
        lines.push(`[×] Python：${py} 调不动 —— ${error?.message ?? error}`);
      }

      // 2. 边车脚本在不在
      const script = resolve(cfg.script);
      if (existsSync(script)) lines.push(`[✓] 边车脚本：${script}`);
      else { ok = false; lines.push(`[×] 边车脚本不在：${script}`); }

      // 2b. 依赖：Pillow 抓图、numpy 算差分（python 起不来就不查，免得同一条错报两遍）
      if (pyOk) try {
        const dep = spawnSync(py, ["-c", "import PIL, numpy; print(PIL.__version__, numpy.__version__)"], {
          timeout: 20000,
          encoding: "utf8",
        });
        if (dep.error || dep.status !== 0) {
          ok = false;
          const why = String(dep.stderr ?? "").trim().split(/\r?\n/).slice(-1)[0] || dep.error?.message || ("退出码 " + dep.status);
          lines.push(`[×] 缺依赖 Pillow / numpy —— ${why}`);
          lines.push("    在插件的 see_now_sidecar.py 所在目录跑一句：pip install -r requirements.txt");
        } else {
          lines.push(`[✓] 依赖：Pillow ${String(dep.stdout ?? "").trim()}`);
        }
      } catch (error: any) {
        ok = false;
        lines.push(`[×] 依赖检查没跑成 —— ${error?.message ?? error}`);
      }

      // 3. 识图这两级各落到谁身上
      const chain = buildChain(ctx, cfg, null);
      if (!chain.length) {
        ok = false;
        lines.push("[×] 识图：一级和二级都没落上，没有能用的模型");
      } else {
        const names = ["一级", "二级", "备用"];
        chain.forEach((leg, i) => {
          lines.push(`[✓] ${names[i] ?? "备用"}识图：${leg.label}`);
        });
        if (chain.some((c) => c.kind === "chat")) {
          lines.push(
            `    共 ${chatModels(ctx).length} 个可用对话模型；选「跟随当前会话」时，谁跟我说话就用谁看图，这里看不到会话，按列第一个算。`,
          );
        }
      }

      // 4. 本地 Ollama（本地是一级就必查；只是备用时查不到只提示，不算失败）
      const isFallback = !(chain.length && chain[0].kind === "ollama");
      try {
        const r = await fetch(`${cfg.ollama}/api/tags`, { signal: AbortSignal.timeout(8000) });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data: any = await r.json();
        const names: string[] = (data?.models ?? []).map((m: any) => String(m?.name ?? ""));
        const want = String(cfg.ollama_model ?? "").trim();
        const hit = names.includes(want) || names.includes(want + ":latest") || names.some((n) => n.startsWith(want + ":"));
        if (hit) lines.push(`[✓] 本地模型 ${want} 在 Ollama 里（共 ${names.length} 个模型）`);
        else if (isFallback) lines.push(`[!] 本地退路 ${want} 不在 Ollama 里，对话模型看不了图时没人接手。现有：${names.slice(0, 8).join("、") || "一个都没有"}`);
        else {
          ok = false;
          lines.push(`[×] 本地模型 ${want} 不在 Ollama 里。现有：${names.slice(0, 8).join("、") || "一个都没有"}`);
        }
      } catch (error: any) {
        if (isFallback) lines.push(`[!] 连不上 Ollama（${cfg.ollama}）：${error?.message ?? error}；只影响退路`);
        else {
          ok = false;
          lines.push(`[×] 连不上 Ollama（${cfg.ollama}）：${error?.message ?? error}`);
        }
      }

      // 5. 出图目录能不能建
      try {
        await mkdir(cfg.shot_dir, { recursive: true });
        lines.push(`[✓] 出图目录：${cfg.shot_dir}`);
      } catch (error: any) {
        ok = false;
        lines.push(`[×] 出图目录建不出来：${cfg.shot_dir} —— ${error?.message ?? error}`);
      }

      lines.push("");
      lines.push("这只是配置检查。真要出图，用 see_now 抓一段。");
      return reply(200, { ok, lines });
    } catch (error: any) {
      return reply(500, { ok: false, lines: [`检查本身出错：${error?.message ?? error}`] });
    }
  });

  ctx.web.page(
    "/plugins/peek/",
    async (_req, res) => {
      await reload();
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
      const warns = (await eyesRunning())
        ? [
            "eyes 插件也开着。它默认把本地 qwen3-vl:4b 视觉模型拉进显卡，和本插件同时用、显存又不够时，整机可能卡死（本机实测过）。留一个就够了，建议到插件管理里停用 eyes。",
          ]
        : [];
      res.end(renderPage(cfg, chatModels(ctx), warns));
    },
    { title: "看屏", navigation: true },
  );

  ctx.web.api("/plugins/peek/save", async (req, res) => {
    try {
      const patchIn = sanitize(await readBody(req));
      const keys = Object.keys(patchIn);
      if (!keys.length) throw new Error("没有可保存的字段");
      const merged = { ...(await ctx.config.read<any>("config.json").catch(() => null)), ...patchIn };
      await ctx.config.save("config.json", merged);
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
      res.end(JSON.stringify({ ok: true, saved: keys }));
    } catch (error: any) {
      res.writeHead(400, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
      res.end(JSON.stringify({ ok: false, error: error?.message ?? String(error) }));
    }
  });

  ctx.effect(() => () => {
    ctx.logger.info("peek 已清理");
  });
}
