param()
$ErrorActionPreference = 'Stop'

# ---- 内联 C#：抓屏、窗口枚举、缩放、网格 ----
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Text;

public class PeekWin {
    public IntPtr Hwnd;
    public int W, H;
    public int X, Y;
    public int Pid;
    public string Proc;
    public string Title;
    public bool Iconic;
    public bool Hung;
}

public class PeekShotLib {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsHungAppWindow(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern int GetWindowTextLength(IntPtr h);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
    delegate bool EnumProc(IntPtr h, IntPtr p);
    [DllImport("user32.dll")] static extern int GetSystemMetrics(int i);
    [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr h);
    [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr h, IntPtr dc);
    [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleDC(IntPtr dc);
    [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleBitmap(IntPtr dc, int w, int h);
    [DllImport("gdi32.dll")] static extern IntPtr SelectObject(IntPtr dc, IntPtr o);
    [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr o);
    [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr dc);
    [DllImport("user32.dll")] static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("shcore.dll")] static extern int SetProcessDpiAwareness(int v);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr h);

    public static string DpiAware() {
        try { if (SetProcessDpiAwareness(2) == 0) return "per-monitor"; } catch (Exception) { }
        try { if (SetProcessDPIAware()) return "system"; } catch (Exception) { }
        return "none";
    }

    // 虚拟桌面矩形：left, top, width, height
    public static int[] VirtualScreen() {
        return new int[] {
            GetSystemMetrics(76), GetSystemMetrics(77),
            GetSystemMetrics(78), GetSystemMetrics(79)
        };
    }

    public static Bitmap GrabRect(int x, int y, int w, int h) {
        if (w <= 0 || h <= 0) throw new ArgumentException("抓屏尺寸无效：" + w + "x" + h);
        Bitmap b = new Bitmap(w, h, PixelFormat.Format24bppRgb);
        using (Graphics g = Graphics.FromImage(b)) {
            g.CopyFromScreen(x, y, 0, 0, new Size(w, h), CopyPixelOperation.SourceCopy);
        }
        return b;
    }

    public static Bitmap GrabWindow(IntPtr hwnd, int w, int h) {
        if (w <= 0 || h <= 0) throw new ArgumentException("窗口尺寸无效：" + w + "x" + h);
        // DPI 感知进程里，GetWindowRect 给的是物理像素，而 PrintWindow 让窗口按逻辑像素画。
        // 直接按物理尺寸建 DC，画面只会占左上角一块，剩下全黑——判分时那一圈黑把分数稀释掉。
        // 所以先按逻辑尺寸抓，再放大回物理尺寸。
        double s = 1.0;
        try {
            uint dpi = GetDpiForWindow(hwnd);
            if (dpi >= 96) s = dpi / 96.0;
        } catch (Exception) { }
        int lw = Math.Max(1, (int)Math.Round(w / s));
        int lh = Math.Max(1, (int)Math.Round(h / s));
        IntPtr screenDc = GetDC(IntPtr.Zero);
        IntPtr memDc = CreateCompatibleDC(screenDc);
        IntPtr hbmp = CreateCompatibleBitmap(screenDc, lw, lh);
        IntPtr old = SelectObject(memDc, hbmp);
        bool ok = PrintWindow(hwnd, memDc, 2u);
        SelectObject(memDc, old);
        Bitmap b = null;
        if (ok) {
            using (Bitmap raw = Image.FromHbitmap(hbmp)) {
                b = new Bitmap(w, h, PixelFormat.Format24bppRgb);
                using (Graphics g = Graphics.FromImage(b)) {
                    g.InterpolationMode = InterpolationMode.HighQualityBicubic;
                    g.DrawImage(raw, 0, 0, w, h);
                }
            }
        }
        DeleteObject(hbmp);
        DeleteDC(memDc);
        ReleaseDC(IntPtr.Zero, screenDc);
        if (b == null) throw new Exception("PrintWindow 取不到画面");
        return b;
    }

    public static List<PeekWin> ListWindows() {
        List<PeekWin> list = new List<PeekWin>();
        EnumWindows(delegate (IntPtr h, IntPtr p) {
            if (!IsWindowVisible(h)) return true;
            RECT r;
            if (!GetWindowRect(h, out r)) return true;
            int w = r.Right - r.Left, hh = r.Bottom - r.Top;
            if (w < 80 || hh < 60) return true;
            int len = GetWindowTextLength(h);
            if (len <= 0) return true;
            StringBuilder sb = new StringBuilder(len + 2);
            GetWindowText(h, sb, sb.Capacity);
            uint pid;
            GetWindowThreadProcessId(h, out pid);
            string proc = "";
            try { proc = Process.GetProcessById((int)pid).ProcessName; } catch (Exception) { }
            PeekWin info = new PeekWin();
            info.Hwnd = h; info.W = w; info.H = hh;
            info.X = r.Left; info.Y = r.Top;
            info.Pid = (int)pid; info.Proc = proc; info.Title = sb.ToString();
            info.Iconic = IsIconic(h);
            info.Hung = IsHungAppWindow(h);
            list.Add(info);
            return true;
        }, IntPtr.Zero);
        return list;
    }

    // 按进程名或标题片段挑窗口：可见、非最小化、不挂起，取面积最大
    public static PeekWin PickApp(string spec, out int total, out int skipped) {
        total = 0; skipped = 0;
        string s = (spec == null) ? "" : spec.Trim().ToLower();
        if (s.Length == 0) return null;
        List<PeekWin> all = ListWindows();
        List<PeekWin> hit = new List<PeekWin>();
        foreach (PeekWin x in all) {
            string proc = x.Proc == null ? "" : x.Proc.ToLower();
            string title = x.Title == null ? "" : x.Title.ToLower();
            if (proc.Contains(s) || title.Contains(s)) hit.Add(x);
        }
        total = hit.Count;
        if (total == 0) return null;
        List<PeekWin> usable = new List<PeekWin>();
        foreach (PeekWin x in hit) {
            if (x.Iconic || x.Hung) { skipped++; continue; }
            usable.Add(x);
        }
        if (usable.Count == 0) return null;
        usable.Sort(delegate (PeekWin a, PeekWin b) { return (b.W * b.H).CompareTo(a.W * a.H); });
        return usable[0];
    }

    public static PeekWin Foreground() {
        IntPtr h = GetForegroundWindow();
        RECT r;
        if (h == IntPtr.Zero || !GetWindowRect(h, out r)) return null;
        PeekWin info = new PeekWin();
        info.Hwnd = h; info.W = r.Right - r.Left; info.H = r.Bottom - r.Top;
        info.X = r.Left; info.Y = r.Top;
        uint pid;
        GetWindowThreadProcessId(h, out pid);
        info.Pid = (int)pid;
        try { info.Proc = Process.GetProcessById((int)pid).ProcessName; } catch (Exception) { }
        int len = GetWindowTextLength(h);
        StringBuilder sb = new StringBuilder(len + 2);
        GetWindowText(h, sb, sb.Capacity);
        info.Title = sb.ToString();
        info.Iconic = IsIconic(h);
        info.Hung = IsHungAppWindow(h);
        return info;
    }

    // 等比缩到指定宽（高按比例），宽高都封顶
    public static Bitmap Fit(Bitmap src, int maxW, int maxH) {
        double s = 1.0;
        if (maxW > 0) s = Math.Min(s, (double)maxW / src.Width);
        if (maxH > 0) s = Math.Min(s, (double)maxH / src.Height);
        int w = Math.Max(1, (int)Math.Round(src.Width * s));
        int h = Math.Max(1, (int)Math.Round(src.Height * s));
        Bitmap b = new Bitmap(w, h, PixelFormat.Format24bppRgb);
        using (Graphics g = Graphics.FromImage(b)) {
            g.InterpolationMode = InterpolationMode.HighQualityBicubic;
            g.PixelOffsetMode = PixelOffsetMode.HighQuality;
            g.DrawImage(src, 0, 0, w, h);
        }
        return b;
    }

    // 在图上叠坐标网格。标签写屏幕像素（origin + 图上像素 * scale）
    public static int DrawGrid(Bitmap b, int originX, int originY, double scale, int step) {
        int lines = 0;
        using (Graphics g = Graphics.FromImage(b)) {
            g.SmoothingMode = SmoothingMode.None;
            g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
            using (Pen pen = new Pen(Color.FromArgb(110, 255, 60, 60), 1))
            using (SolidBrush bg = new SolidBrush(Color.FromArgb(190, 0, 0, 0)))
            using (SolidBrush fg = new SolidBrush(Color.FromArgb(255, 255, 210, 90)))
            using (Font f = new Font("Consolas", 9f)) {
                int sx = (step > 0) ? step : Math.Max(40, b.Width / 8);
                int sy = (step > 0) ? step : Math.Max(40, b.Height / 6);
                for (int x = sx; x < b.Width; x += sx) {
                    g.DrawLine(pen, x, 0, x, b.Height);
                    lines++;
                    int scr = originX + (int)Math.Round(x * scale);
                    g.DrawString(scr.ToString(), f, fg, x + 2, 2);
                }
                for (int y = sy; y < b.Height; y += sy) {
                    g.DrawLine(pen, 0, y, b.Width, y);
                    lines++;
                    int scr = originY + (int)Math.Round(y * scale);
                    g.DrawString(scr.ToString(), f, fg, 2, y + 1);
                }
            }
        }
        return lines;
    }

    // 缩到小图后算灰度向量，给后面差分用
    public static byte[] Gray(Bitmap src, int w, int h, out int outW, out int outH) {
        using (Bitmap small = new Bitmap(w, h, PixelFormat.Format24bppRgb)) {
            using (Graphics g = Graphics.FromImage(small)) {
                g.InterpolationMode = InterpolationMode.HighQualityBilinear;
                g.DrawImage(src, 0, 0, w, h);
            }
            outW = w; outH = h;
            BitmapData d = small.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.ReadOnly, PixelFormat.Format24bppRgb);
            byte[] buf = new byte[d.Stride * h];
            try { Marshal.Copy(d.Scan0, buf, 0, buf.Length); }
            finally { small.UnlockBits(d); }
            byte[] gray = new byte[w * h];
            for (int y = 0; y < h; y++) {
                int row = y * d.Stride;
                for (int x = 0; x < w; x++) {
                    int i = row + x * 3;
                    gray[y * w + x] = (byte)((buf[i] * 29 + buf[i + 1] * 150 + buf[i + 2] * 77) >> 8);
                }
            }
            return gray;
        }
    }

    // ---- 判分用的小图：缩到 GRID，输出 RGB 三字节一组（源是 BGR，这里倒过来）----
    public static byte[] Small(Bitmap src, int w, int h) {
        using (Bitmap s2 = new Bitmap(w, h, PixelFormat.Format24bppRgb)) {
            using (Graphics g = Graphics.FromImage(s2)) {
                g.InterpolationMode = InterpolationMode.Bilinear;
                g.PixelOffsetMode = PixelOffsetMode.Half;
                g.DrawImage(src, 0, 0, w, h);
            }
            BitmapData d = s2.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.ReadOnly, PixelFormat.Format24bppRgb);
            byte[] buf = new byte[d.Stride * h];
            try { Marshal.Copy(d.Scan0, buf, 0, buf.Length); } finally { s2.UnlockBits(d); }
            byte[] o = new byte[w * h * 3];
            for (int y = 0; y < h; y++) {
                int row = y * d.Stride;
                for (int x = 0; x < w; x++) {
                    int i = row + x * 3; int k = (y * w + x) * 3;
                    o[k] = buf[i + 2]; o[k + 1] = buf[i + 1]; o[k + 2] = buf[i];
                }
            }
            return o;
        }
    }

    static void ToHsv(byte r8, byte g8, byte b8, out double h, out double s, out double v) {
        double r = r8, g = g8, b = b8;
        double mx = Math.Max(r, Math.Max(g, b)), mn = Math.Min(r, Math.Min(g, b));
        double d = mx - mn;
        v = mx;
        s = (mx == 0) ? 0 : d / mx * 255.0;
        if (d == 0) { h = 0; return; }
        if (mx == r) h = (g - b) / d * 255.0 / 6.0;
        else if (mx == g) h = (2.0 / 6.0 + (b - r) / d / 6.0) * 255.0;
        else h = (4.0 / 6.0 + (r - g) / d / 6.0) * 255.0;
        if (h < 0) h += 255.0;
    }

    // 0-255 的变化分：HSV 三通道加权，H 走环形
    public static double Score(byte[] a, byte[] b) {
        int n = a.Length / 3;
        if (n == 0) return 0;
        double sum = 0;
        for (int i = 0; i < n; i++) {
            int p = i * 3;
            double h1, s1, v1, h2, s2, v2;
            ToHsv(a[p], a[p + 1], a[p + 2], out h1, out s1, out v1);
            ToHsv(b[p], b[p + 1], b[p + 2], out h2, out s2, out v2);
            double dh = Math.Abs(h1 - h2); if (dh > 255.0 - dh) dh = 255.0 - dh;
            sum += 0.20 * dh + 0.25 * Math.Abs(s1 - s2) + 0.55 * Math.Abs(v1 - v2);
        }
        return sum / n;
    }

    // 拼图：按档位分格（1 档独占整行，2、3 档一行放两三格），角标写相对秒数与缩放比
    public static int Pack(string[] paths, double[] secs, int[] tiers, double[] scales, string outPath, int fullW, int maxH) {
        int n = paths.Length;
        if (n == 0) return 0;
        List<Bitmap> srcs = new List<Bitmap>();
        List<double> ratios = new List<double>();
        for (int i = 0; i < n; i++) {
            Bitmap t = new Bitmap(paths[i]);
            srcs.Add(t);
            ratios.Add((double)t.Height / t.Width);
        }
        List<List<int>> rows = new List<List<int>>();
        List<int> cur = new List<int>(); int curK = 0;
        for (int i = 0; i < n; i++) {
            int k = Math.Max(1, Math.Min(3, tiers[i]));
            if (cur.Count > 0 && k != curK) { rows.Add(cur); cur = new List<int>(); }
            if (cur.Count >= k) { rows.Add(cur); cur = new List<int>(); }
            curK = k; cur.Add(i);
        }
        if (cur.Count > 0) rows.Add(cur);
        int totalH = 0;
        List<int> rowH = new List<int>();
        for (int r = 0; r < rows.Count; r++) {
            List<int> idx = rows[r];
            int k = Math.Max(1, Math.Min(3, tiers[idx[0]]));
            int cellW = Math.Max(1, fullW / k);
            int h = 0;
            for (int j = 0; j < idx.Count; j++) h = Math.Max(h, (int)Math.Round(cellW * ratios[idx[j]]));
            rowH.Add(h); totalH += h;
        }
        int keepRows = rows.Count;
        while (keepRows > 1 && totalH > maxH) { keepRows--; totalH -= rowH[keepRows]; }
        if (totalH <= 0) { foreach (Bitmap t in srcs) t.Dispose(); return 0; }
        Bitmap canvas = new Bitmap(fullW, totalH, PixelFormat.Format24bppRgb);
        int drawn = 0;
        using (Graphics g = Graphics.FromImage(canvas)) {
            g.InterpolationMode = InterpolationMode.HighQualityBicubic;
            g.Clear(Color.FromArgb(0, 0, 0));
            int y = 0;
            for (int r = 0; r < keepRows; r++) {
                List<int> idx = rows[r];
                int k = Math.Max(1, Math.Min(3, tiers[idx[0]]));
                int cellW = Math.Max(1, fullW / k);
                for (int j = 0; j < idx.Count; j++) {
                    int i = idx[j];
                    int h = (int)Math.Round(cellW * ratios[i]);
                    g.DrawImage(srcs[i], j * cellW, y, cellW, h);
                    using (SolidBrush bg = new SolidBrush(Color.FromArgb(190, 0, 0, 0)))
                    using (SolidBrush fg = new SolidBrush(Color.FromArgb(255, 255, 210, 90)))
                    using (Font f = new Font("Consolas", 9f)) {
                        string label = "+" + secs[i].ToString("0.0") + "s x" + scales[i].ToString("0.0");
                        SizeF sz = g.MeasureString(label, f);
                        g.FillRectangle(bg, j * cellW + 1, y + 1, sz.Width + 4, sz.Height + 1);
                        g.DrawString(label, f, fg, j * cellW + 3, y + 2);
                    }
                    drawn++;
                }
                y += rowH[r];
            }
        }
        canvas.Save(outPath, ImageFormat.Png);
        canvas.Dispose();
        foreach (Bitmap t in srcs) t.Dispose();
        return drawn;
    }

    public static string Version() { return "2"; }
}
'@ -ReferencedAssemblies System.Drawing

$aware = [PeekShotLib]::DpiAware()

# ---- 参数 ----
$opts = @{}
for ($i = 0; $i -lt $args.Count; $i++) {
    $a = $args[$i]
    if ($a -like '--*') {
        $k = $a.Substring(2)
        $v = $null
        if (($i + 1) -lt $args.Count -and $args[$i + 1] -notlike '--*') { $v = $args[$i + 1]; $i++ }
        $opts[$k] = $v
    }
}

function Opt([string]$name, $default) {
    if ($opts.ContainsKey($name) -and $null -ne $opts[$name]) { return $opts[$name] }
    return $default
}

$target = [string](Opt 'target' 'screen')
$appSpec = [string](Opt 'app' '')
$width = [int](Opt 'width' 800)
$maxH = [int](Opt 'max-h' 0)
$seconds = [double](Opt 'seconds' 10)
$maxFrames = [int](Opt 'max-frames' 6)
$cutAbsRaw = [double](Opt 'cut-abs' 0)
$needRaw = [double](Opt 'need' 0)
$cutAbs = $(if ($cutAbsRaw -gt 0) { $cutAbsRaw } else { 12.0 })
$need = $(if ($needRaw -gt 0) { $needRaw } else { 144.0 })
$appRelax = [double](Opt 'app-relax' 0.6)
$relaxed = $false
if ($target -eq 'app' -and $cutAbsRaw -le 0 -and $needRaw -le 0 -and $appRelax -gt 0 -and $appRelax -lt 1) {
    $cutAbs = [Math]::Round($cutAbs * $appRelax, 2)
    $need = [Math]::Round($need * $appRelax, 2)
    $relaxed = $true
}
if ($seconds -lt 1) { $seconds = 1 }
if ($seconds -gt 60) { $seconds = 60 }
if ($maxFrames -lt 1) { $maxFrames = 1 }
if ($maxFrames -gt 12) { $maxFrames = 12 }
$dbg = [string](Opt 'debug' '')
$gridArg = [string](Opt 'grid' 'auto')
$shotDir = [string](Opt 'out' '')
if ([string]::IsNullOrWhiteSpace($shotDir)) { $shotDir = Join-Path $PSScriptRoot 'shots' }
if (-not (Test-Path $shotDir)) { New-Item -ItemType Directory -Path $shotDir -Force | Out-Null }

$result = @{
    ok = $false
    status = 'error'
    target = $target
    dpi = $aware
    shot = $null
    origin = @(0, 0)
    screen = @(0, 0)
    scale = 1.0
    note = ''
    image = @{ w = 0; h = 0 }
    grid = @{ step = 0; lines = 0 }
}

function TierOf([double]$s) {
    if ($s -ge 45.0) { return 1 }
    if ($s -ge 18.0) { return 2 }
    return 3
}

$script:grabNote = ''
function GrabOnce {
    if ($target -eq 'app') {
        $tot = 0; $sk = 0
        $win = [PeekShotLib]::PickApp($appSpec, [ref]$tot, [ref]$sk)
        if ($null -ne $win) {
            $b = [PeekShotLib]::GrabWindow($win.Hwnd, $win.W, $win.H)
            $script:grabNote = "抓窗口《$($win.Title)》（进程 $($win.Proc)，$($win.W)x$($win.H)）"
            return @{ bmp = $b; origin = @($win.X, $win.Y) }
        }
        $script:grabNote = "没找到可用窗口（匹配 $tot 个），退回整屏"
    }
    elseif ($target -eq 'window') {
        $win = [PeekShotLib]::Foreground()
        if ($null -ne $win -and -not $win.Iconic) {
            $b = [PeekShotLib]::GrabRect($win.X, $win.Y, $win.W, $win.H)
            $script:grabNote = "抓前台窗口《$($win.Title)》（$($win.W)x$($win.H)）"
            return @{ bmp = $b; origin = @($win.X, $win.Y) }
        }
        $script:grabNote = "前台窗口拿不到，退回整屏"
    }
    $vs = [PeekShotLib]::VirtualScreen()
    $b = [PeekShotLib]::GrabRect($vs[0], $vs[1], $vs[2], $vs[3])
    if ($script:grabNote -eq '') { $script:grabNote = "抓一整屏（$($vs[2])x$($vs[3])）" }
    return @{ bmp = $b; origin = @($vs[0], $vs[1]) }
}

try {
    $GRIDW = 64; $GRIDH = 36
    $fps = 6
    $refract = 0.4
    $cutPeak = 2.5

    $t0 = Get-Date
    $nextAt = 0.0
    $shots = 0
    $frames = New-Object System.Collections.ArrayList
    $prevSmall = $null
    $prevScore = 1.0
    $integ = 0.0
    $lastCut = 0.0
    $lastBmp = $null
    $lastOrigin = @(0, 0)
    $notes = @()
    $frameDir = Join-Path $shotDir 'frames'
    if (-not (Test-Path $frameDir)) { New-Item -ItemType Directory -Path $frameDir -Force | Out-Null }

    while ($true) {
        if (((Get-Date) - $t0).TotalSeconds -ge $seconds) { break }
        if ($frames.Count -ge $maxFrames) { break }
        $nextAt += 1.0 / $fps
        $wait = $nextAt - ((Get-Date) - $t0).TotalSeconds
        if ($wait -gt 0) { Start-Sleep -Milliseconds ([int]($wait * 1000)) }

        $g = GrabOnce
        $bmp = $g.bmp
        if ($null -eq $bmp) { break }
        if ($null -ne $lastBmp -and -not [object]::ReferenceEquals($lastBmp, $bmp)) { $lastBmp.Dispose() }
        $lastBmp = $bmp
        $lastOrigin = $g.origin
        if ($script:grabNote -and ($notes -notcontains $script:grabNote)) { $notes += $script:grabNote }
        $shots++

        $small = [PeekShotLib]::Small($bmp, $GRIDW, $GRIDH)
        $s = 0.0
        if ($null -ne $prevSmall) { $s = [PeekShotLib]::Score($prevSmall, $small) }
        $now = ((Get-Date) - $t0).TotalSeconds

        if ($dbg) {
            $preview = "", ""
            if ($null -ne $small) { $preview = "$($small[0]),$($small[1]),$($small[2])" }
            [Console]::Error.WriteLine(('[dbg] t={0} s={1} px={2} bmp={3}x{4}' -f [Math]::Round($now, 2), [Math]::Round($s, 2), $preview, $bmp.Width, $bmp.Height))
        } # WROTE-DBG
        $keep = $false
        if ($null -ne $prevSmall) {
            if ($s -ge $cutAbs -and $s -gt ($cutPeak * [Math]::Max(1.0, $prevScore)) -and ($now - $lastCut) -ge $refract) {
                $keep = $true
            }
            else {
                $integ += $s
                if ($integ -ge $need) { $keep = $true }
            }
        }

        if ($keep) {
            $scaled = [PeekShotLib]::Fit($bmp, $width, 0)
            $fp = Join-Path $frameDir ("f{0:D2}.png" -f $frames.Count)
            $scaled.Save($fp, [System.Drawing.Imaging.ImageFormat]::Png)
            $scaled.Dispose()
            $null = $frames.Add(@{ t = $now; score = $s; path = $fp })
            $lastCut = $now
            $integ = 0.0
        }

        $prevSmall = $small
        $prevScore = $s
    }

    $result.shots = $shots
    $result.kept = $frames.Count
    $result.seconds = $seconds
    $result.target = $target
    $result.relax = $relaxed

    if ($null -eq $lastBmp) { throw '一帧都没抓到' }

    $srcW = $lastBmp.Width
    $srcH = $lastBmp.Height
    $result.screen = @($srcW, $srcH)
    $result.origin = $lastOrigin
    $scale = [double]$srcW / [double]$width
    $result.scale = [Math]::Round($scale, 3)

    $stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
    $outPath = Join-Path $shotDir ("see_now_{0}.png" -f $stamp)
    $rawPath = Join-Path $shotDir ("_raw_{0}.png" -f $stamp)
    $reused = $false
    $packed = 0
    $sizeW = 0; $sizeH = 0

    if ($frames.Count -gt 1) {
        $paths = @(); $secs = @(); $tiers = @(); $scales = @()
        foreach ($f in $frames) {
            $paths += [string]$f.path
            $secs += [double]$f.t
            $tiers += (TierOf ([double]$f.score))
            $scales += $scale
        }
        $ratio = [double]$srcH / [double]$srcW
        $maxPackH = [Math]::Max(450, [int][Math]::Round($width * $ratio * 3))
        if ($maxH -gt 0) { $maxPackH = $maxH }
        $packed = [PeekShotLib]::Pack($paths, $secs, $tiers, $scales, $rawPath, $width, $maxPackH)
        $img = [System.Drawing.Image]::FromFile($rawPath)
        $sizeW = $img.Width; $sizeH = $img.Height
        $img.Dispose()
    }
    elseif ($frames.Count -eq 1) {
        $scaled = [PeekShotLib]::Fit($lastBmp, $width, $(if ($maxH -gt 0) { $maxH } else { 0 }))
        $scaled.Save($rawPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $sizeW = $scaled.Width; $sizeH = $scaled.Height
        $scaled.Dispose()
        $packed = 1
    }
    else {
        $scaled = [PeekShotLib]::Fit($lastBmp, $width, $(if ($maxH -gt 0) { $maxH } else { 0 }))
        $scaled.Save($rawPath, [System.Drawing.Imaging.ImageFormat]::Png)
        $sizeW = $scaled.Width; $sizeH = $scaled.Height
        $scaled.Dispose()
        $reused = $true
        $packed = 1
    }

    $final = [System.Drawing.Bitmap]::FromFile($rawPath)
    $gridLines = 0
    if ($gridArg -ne '0' -and $gridArg -ne '') {
        $stepPx = 0
        if ($gridArg -ne 'auto') {
            $parsed = 0
            if ([int]::TryParse($gridArg, [ref]$parsed) -and $parsed -gt 0) { $stepPx = $parsed }
        }
        $gridLines = [PeekShotLib]::DrawGrid($final, $lastOrigin[0], $lastOrigin[1], $scale, $stepPx)
        $result.grid = @{ step = $stepPx; lines = $gridLines }
    }
    $final.Save($outPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $final.Dispose()

    $result.ok = $true
    $result.status = 'ok'
    $result.shot = $outPath
    $result.image = @{ w = $sizeW; h = $sizeH }
    $result.frames = $packed
    $result.reused = $reused
    if ($frames.Count -gt 0) {
        $tiersList = @()
        foreach ($f in $frames) { $tiersList += (TierOf ([double]$f.score)) }
        $result.tier = ($tiersList -join ',')
    }

    $noteParts = @()
    if ($notes.Count -gt 0) { $noteParts += ($notes -join '；') }
    $noteParts += "抓了 $shots 帧，留下 $($frames.Count) 帧"
    if ($relaxed) { $noteParts += "app 档阈值放宽到原值的 $appRelax 倍（突变 $cutAbs、积分 $need），窗口黑边会稀释分数" }
    if ($reused) { $noteParts += '画面几乎没动，这一张是刚刚现抓的，不是旧图' }
    if ($gridLines -gt 0) { $noteParts += '叠了坐标网格，刻度数字是屏幕像素' }
    if ($frames.Count -gt 1) { $noteParts += "拼成一张（$sizeW x $sizeH），左上角标着 +秒数 x缩放" }
    $result.note = ($noteParts -join '。') + "。原屏 $($srcW)x$($srcH)。"

    if (Test-Path $rawPath) { Remove-Item $rawPath -Force }
    if ($null -ne $lastBmp) { $lastBmp.Dispose() }
}
catch {
    $result.status = 'error'
    $result.note = '抓屏失败：' + $_.Exception.Message
}
$result | ConvertTo-Json -Depth 6 -Compress
