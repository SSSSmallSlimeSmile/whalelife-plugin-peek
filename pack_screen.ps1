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
  [long]$WindowHandle = 0
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
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  public struct RECT { public int Left, Top, Right, Bottom; }
}
"@

$sw = [System.Diagnostics.Stopwatch]::StartNew()
$out = [ordered]@{ ok = $true; at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }

# ---- 一、窗口与控件 ----
$swUia = [System.Diagnostics.Stopwatch]::StartNew()
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
