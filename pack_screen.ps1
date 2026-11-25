# 屏幕 -> 文字与坐标。两条腿，都不装东西：
#   一、UI Automation：问窗口要控件清单（原生程序、对话框、资源管理器管用）
#   二、Windows.Media.Ocr：从截图里抠字，连每个词的框一起给（网页、游戏、画布靠这条）
# 输出一份 JSON，模型读的是字，不是图。
param(
  [string]$Path = "",
  [int]$MaxElements = 60,
  [int]$MaxLines = 120,
  # 只看这个窗口的控件树。0 = 老行为，取当前前台窗口。
  # 抓的是指定窗口时前台可能是别人，那就把别的窗口的控件一起报出去了。
  [long]$WindowHandle = 0,
  # 窗口身份确认不了就别读控件树：宁可只给图上的字，也不给错窗的字。
  [switch]$SkipUia,
  # 窗口身份指纹（pid / 类名 / 标题）。传了就核对：句柄会被系统回收再分配给
  # 另一个窗口，号码一样、内容已经不是，那读出来的控件就是别人家的。
  [int]$ExpectPid = 0,
  [string]$ExpectClass = "",
  [string]$ExpectTitle = "",
  # 身份也可以走这份 UTF-8 JSON（pid / cls / title）。窗口标题是中文，
  # 经命令行传过来会被换掉编码，于是同一个窗口被判成对不上——所以走文件。
  [string]$ExpectJson = ""
)

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Runtime.WindowsRuntime

# 管道交出去的一律按 UTF-8 出，否则中文在 Python 那头会变成一锅乱码。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null

Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class Win32Probe {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr h, out int pid);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$out = [ordered]@{ ok = $true; at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }

# ---- 一、窗口与控件 ----
$swUia = [System.Diagnostics.Stopwatch]::StartNew()

# 身份先核：句柄号一样不等于还是那个窗口。对不上就改写 $SkipUia，
# 走现成那条「跳过控件、只给图上的字」的路，不另起一套。
if ($ExpectJson -and (Test-Path -LiteralPath $ExpectJson)) {
  try {
    $j = Get-Content -LiteralPath $ExpectJson -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($j.pid) { $ExpectPid = [int]$j.pid }
    if ($j.cls) { $ExpectClass = [string]$j.cls }
    if ($j.title) { $ExpectTitle = [string]$j.title }
    $out.expect_from = "json"
  } catch {
    $out.uia_blocked = ("身份文件读不出（" + $_.Exception.Message + "），跳过控件读取")
    $SkipUia = $true
  }
}

# 身份够不够：标题非空才算；标题空的，要求 pid 和类名都在。
# 只给 pid 不够——同进程可以有好几个窗口，句柄复用给同进程另一个窗口时 pid 照样对。
# 标题要先 trim：纯空白的标题 [bool] 也是真，那等于把「没有标题」当成强身份。
# Python 那头是 strip 过再判的，两边判据必须一样。
$TitleTrim = ("" + $ExpectTitle).Trim()
$ClassTrim = ("" + $ExpectClass).Trim()
$HasExpect  = (($ExpectPid -ne 0) -or [bool]$ClassTrim -or [bool]$TitleTrim)
$HaveStrong = [bool]$TitleTrim
$HaveWeak   = (($ExpectPid -ne 0) -and [bool]$ClassTrim)

# 要核身份却没句柄：核不了，跳过。这一档只对「要求核身份」的调用生效；
# 没给身份的（整屏档）照旧读前台窗口，那是它本来的语义——要不要读前台由
# Python 那头按 target 决定，这里不替它猜。
if (-not $SkipUia -and $HasExpect -and $WindowHandle -eq 0) {
  $out.uia_blocked = "要核窗口身份却没有句柄，跳过控件读取"
  $SkipUia = $true
}

if (-not $SkipUia -and $HasExpect -and -not ($HaveStrong -or $HaveWeak)) {
  $out.uia_blocked = "这次没拿到可用的窗口身份（既没有标题，也没有进程+类名），跳过控件读取"
  $SkipUia = $true
}

# 给了句柄，却一项身份都没有：核不了，就不知道读到的还是不是那个窗口。
if (-not $SkipUia -and $WindowHandle -ne 0 -and -not $HasExpect) {
  $out.uia_blocked = "给了句柄但没有窗口身份可核，跳过控件读取"
  $SkipUia = $true
}

if (-not $SkipUia -and $HasExpect -and (-not $HaveStrong) -and $HaveWeak) {
  $out.identity_weak = "标题为空，按 pid + 类名核验（同进程多窗口时分不开）"
}

if (-not $SkipUia -and ($ExpectPid -ne 0 -or $ExpectClass -or $ExpectTitle)) {
  try {
    $hk = [IntPtr]$WindowHandle
    if ($WindowHandle -eq 0) { $hk = [Win32Probe]::GetForegroundWindow() }
    $clsNow = New-Object System.Text.StringBuilder 256
    [void][Win32Probe]::GetClassName($hk, $clsNow, 256)
    $tNow = New-Object System.Text.StringBuilder 512
    [void][Win32Probe]::GetWindowText($hk, $tNow, 512)
    $pidNow = 0
    [void][Win32Probe]::GetWindowThreadProcessId($hk, [ref]$pidNow)
    $bad = @()
    if ($ExpectPid -ne 0 -and $pidNow -ne $ExpectPid) { $bad += ("进程 " + $pidNow + "，应为 " + $ExpectPid) }
    if ($ClassTrim -and $clsNow.ToString() -ne $ClassTrim) { $bad += ("类名 " + $clsNow.ToString()) }
    if ($TitleTrim -and $tNow.ToString() -ne $TitleTrim) { $bad += ("标题 " + $tNow.ToString()) }
    if ($bad.Count -gt 0) {
      $out.uia_blocked = ("窗口身份对不上（" + ($bad -join "；") + "），跳过控件读取")
      $SkipUia = $true
    }
  } catch {
    $out.uia_blocked = ("窗口身份核验失败（" + $_.Exception.Message + "），跳过控件读取")
    $SkipUia = $true
  }
}

if ($SkipUia) {
  if ($out.uia_blocked) { $out.uia_skipped = $out.uia_blocked }
  else { $out.uia_skipped = "窗口身份无法确认，跳过控件读取" }
}
else {
try {
  if ($WindowHandle -ne 0) { $h = [IntPtr]$WindowHandle } else { $h = [Win32Probe]::GetForegroundWindow() }
  $sb = New-Object System.Text.StringBuilder 512
  [void][Win32Probe]::GetWindowText($h, $sb, 512)
  $rect = New-Object Win32Probe+RECT
  [void][Win32Probe]::GetWindowRect($h, [ref]$rect)

  $out.window = $sb.ToString()
  $out.rect = @{ x = $rect.Left; y = $rect.Top; w = ($rect.Right - $rect.Left); h = ($rect.Bottom - $rect.Top) }

  $items = New-Object System.Collections.ArrayList
  $root = [System.Windows.Automation.AutomationElement]::FromHandle($h)
  $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                       [System.Windows.Automation.Condition]::TrueCondition)
  $n = [Math]::Min($all.Count, 4000)
  for ($i = 0; $i -lt $n; $i++) {
    if ($items.Count -ge $MaxElements) { break }
    $c = $all.Item($i).Current
    if (-not $c.Name -and -not $c.AutomationId) { continue }
    $b = $c.BoundingRectangle
    if ($b.Width -le 0 -or $b.Height -le 0) { continue }
    [void]$items.Add([ordered]@{
      n = $c.Name
      t = ($c.ControlType.ProgrammaticName -replace '^ControlType\.', '')
      x = [int]$b.X; y = [int]$b.Y; w = [int]$b.Width; h = [int]$b.Height
    })
  }
  $out.elements = $items
  $out.element_count = $items.Count
} catch {
  $out.uia_error = $_.Exception.Message
}
}
$swUia.Stop()
$out.uia_ms = $swUia.ElapsedMilliseconds

# ---- 二、OCR ----
$swOcr = [System.Diagnostics.Stopwatch]::StartNew()
if ($Path -and (Test-Path -LiteralPath $Path)) {
  try {
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    function Await($op, $type) {
      $t = $asTaskGeneric.MakeGenericMethod($type).Invoke($null, @($op))
      $t.Wait(-1) | Out-Null
      $t.Result
    }

    $full = (Resolve-Path -LiteralPath $Path).ProviderPath
    $sf = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($full)) ([Windows.Storage.StorageFile])
    $st = Await ($sf.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($st)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) { throw "no OCR language pack" }
    $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])

    $lines = New-Object System.Collections.ArrayList
    foreach ($ln in $res.Lines) {
      if ($lines.Count -ge $MaxLines) { break }
      $ws = @($ln.Words)
      if ($ws.Count -eq 0) { continue }
      $first = $ws[0].BoundingRect
      [void]$lines.Add([ordered]@{
        t = $ln.Text
        x = [int]$first.X; y = [int]$first.Y
      })
    }
    $out.lines = $lines
    $out.line_count = $lines.Count
    $out.ocr_lang = $engine.RecognizerLanguage.LanguageTag
  } catch {
    $out.ocr_error = $_.Exception.Message
  }
} else {
  $out.ocr_error = "no image"
}
$swOcr.Stop()
$out.ocr_ms = $swOcr.ElapsedMilliseconds

$sw.Stop()
$out.elapsed_ms = $sw.ElapsedMilliseconds
$out | ConvertTo-Json -Depth 6 -Compress
