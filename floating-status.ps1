param([string]$ThreadId = '')
$ErrorActionPreference = 'Stop'
$mutex = [Threading.Mutex]::new($false, 'Local\CodexTokenFloatingStatus')
if (-not $mutex.WaitOne(0)) { exit 0 }
$runtime = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
$configPath = Join-Path $runtime 'floating-status.json'
$script:config = @{ thread = ''; offsetX = 0; offsetY = 0 }
if (Test-Path -LiteralPath $configPath) { $script:config = Get-Content $configPath -Raw | ConvertFrom-Json -AsHashtable }
if ($script:config.anchorMode -ne 'conversation-top-v2') {
    $script:config = @{ thread = $script:config.thread; offsetX = 0; offsetY = 0; anchorMode = 'conversation-top-v2' }
}
if ($ThreadId) { $script:config.thread = $ThreadId }
$server = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList @('-NoProfile','-WindowStyle','Hidden','-File',('"'+(Join-Path $PSScriptRoot 'start-panel.ps1')+'"')) -WindowStyle Hidden -PassThru
$server.WaitForExit() # Wait for the launcher only, not its persistent Python server.
if ($server.ExitCode -ne 0) { throw '本地数据服务启动失败，请查看 runtime/server-error.log。' }
Add-Type -AssemblyName PresentationFramework
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class TokenFloatNative {
    [StructLayout(LayoutKind.Sequential)] public struct Rect {public int Left,Top,Right,Bottom;}
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out Rect r);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr z,int x,int y,int w,int t,uint f);
}
'@
[xml]$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" Title="Codex 会话用量细条" WindowStyle="None" ResizeMode="NoResize" ShowInTaskbar="False" ShowActivated="False" Width="440" Height="54" Background="White" FontFamily="Segoe UI, Microsoft YaHei UI" TextOptions.TextFormattingMode="Display" UseLayoutRounding="True">
 <Border BorderBrush="#E3E5E8" BorderThickness="1" Padding="6,3">
  <Grid>
   <Grid.RowDefinitions><RowDefinition Height="20"/><RowDefinition Height="26"/></Grid.RowDefinitions>
   <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="24"/><ColumnDefinition Width="20"/></Grid.ColumnDefinitions>
   <StackPanel x:Name="TitleArea" Grid.ColumnSpan="3" Orientation="Horizontal" HorizontalAlignment="Center" Margin="44,0" Cursor="SizeAll">
    <TextBlock x:Name="TitleText" Text="绑定会话 · 正在连接" Foreground="#62666C" FontSize="12" TextTrimming="CharacterEllipsis" VerticalAlignment="Center" ToolTip="默认位于对话区顶部；拖动可微调，从菜单恢复默认位置。"/>
    <TextBlock x:Name="ModelText" Margin="8,0,0,0" Foreground="#34383E" FontSize="12" VerticalAlignment="Center"/>
   </StackPanel>
   <Button x:Name="MenuButton" Grid.Column="1" Content="⋯" Background="Transparent" Foreground="#697078" BorderThickness="0" ToolTip="选择会话 / 打开详情 / 恢复贴齐"/>
   <Button x:Name="CloseButton" Grid.Column="2" Content="×" Background="Transparent" Foreground="#697078" BorderThickness="0" ToolTip="关闭浮动细条"/>
   <UniformGrid x:Name="MetricsGrid" Grid.Row="1" Grid.ColumnSpan="3" Columns="4" VerticalAlignment="Center" ToolTip="近期是日志采样区间速度；平均包含工具与等待。美元是 API 等值参考，* 表示部分模型未定价。">
    <TextBlock TextAlignment="Center" Foreground="#72767D" FontSize="11"><Run Text="近期 "/><Run x:Name="RecentValue" Text="等待采样" FontSize="12" FontWeight="SemiBold" Foreground="#166B50"/><Run x:Name="RecentUnit" Text="" FontSize="10"/></TextBlock>
    <TextBlock TextAlignment="Center" Foreground="#72767D" FontSize="11"><Run Text="平均 "/><Run x:Name="AverageValue" Text="—" FontSize="15" FontWeight="SemiBold" Foreground="#24272C"/><Run Text=" t/s" FontSize="10"/></TextBlock>
    <TextBlock TextAlignment="Center" Foreground="#72767D" FontSize="11"><Run x:Name="RoundLabel" Text="本轮 "/><Run x:Name="RoundValue" Text="—" FontSize="15" FontWeight="SemiBold" Foreground="#24272C"/></TextBlock>
    <TextBlock TextAlignment="Center" Foreground="#72767D" FontSize="11"><Run Text="累计 "/><Run x:Name="TotalValue" Text="—" FontSize="15" FontWeight="SemiBold" Foreground="#24272C"/></TextBlock>
   </UniformGrid>
   <TextBlock x:Name="MetricsText" Grid.Row="1" Grid.ColumnSpan="3" Background="White" Foreground="#72767D" FontSize="12" TextAlignment="Center" VerticalAlignment="Center" Text="等待本地采样"/>
  </Grid>
 </Border>
</Window>
'@
$script:window = [Windows.Markup.XamlReader]::Load([Xml.XmlNodeReader]::new($xaml))
$script:titleText = $window.FindName('TitleText')
$script:modelText = $window.FindName('ModelText')
$script:metricsText = $window.FindName('MetricsText')
$script:recentValue = $window.FindName('RecentValue')
$script:recentUnit = $window.FindName('RecentUnit')
$script:averageValue = $window.FindName('AverageValue')
$script:roundLabel = $window.FindName('RoundLabel')
$script:roundValue = $window.FindName('RoundValue')
$script:totalValue = $window.FindName('TotalValue')
$script:menuButton = $window.FindName('MenuButton')
$window.FindName('CloseButton').Add_Click({ $script:window.Close() })
$script:interop = [Windows.Interop.WindowInteropHelper]::new($window)
$script:handle = $interop.EnsureHandle()
$script:ownerHandle = [IntPtr]::Zero
$script:pending = $null
$script:lastRequest = [datetime]::MinValue
$script:threads = @()
$script:lastData = $null
$script:dragging = $false
$script:anchor = $null
$script:anchorAt = [datetime]::MinValue
$script:locateJob = $null
$script:locator = [powershell]::Create()
$script:lastLocate = [datetime]::MinValue
# Read geometry only; background UIA queries never block the WPF dispatcher.
$script:locateScript = @'
param([long]$owner)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
try {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$owner)
    $condition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::ControlTypeProperty,[System.Windows.Automation.ControlType]::Edit)
    foreach ($edit in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,$condition)) {
        if ($edit.Current.ClassName -notlike 'ProseMirror*' -or $edit.Current.IsOffscreen) { continue }
        $parent = [System.Windows.Automation.TreeWalker]::RawViewWalker.GetParent($edit)
        if ($parent.Current.ClassName -notlike 'ComposerLayoutBody-*') { continue }
        $r = $parent.Current.BoundingRectangle
        if ($r.Width -gt 150 -and $r.Height -gt 30 -and $r.Height -lt 600) { return @($r.X,$r.Y,$r.Width,$r.Height) }
    }
} catch [System.Windows.Automation.ElementNotAvailableException] {
} catch [System.Runtime.InteropServices.COMException] {
}
'@
$handler = [Net.Http.HttpClientHandler]::new()
$handler.UseProxy = $false
$script:client = [Net.Http.HttpClient]::new($handler)
$client.Timeout = [TimeSpan]::FromSeconds(3)
function Save-FloatingConfig { $script:config | ConvertTo-Json | Set-Content -LiteralPath $configPath }
function Format-Rate($n) { if ($null -eq $n) { '—' } else { '{0:F1}' -f $n } }
function Format-USD($n) { if ($null -eq $n) { '待定价' } else { '$' + ('{0:F4}' -f $n) } }
function Render-FloatingData($data) {
    $d = $data.selected
    if (-not $d) {
        $script:titleText.Text = '绑定会话不可读 · 请从 ⋯ 选择会话'
        $script:modelText.Text = ''
        $script:metricsText.Text = '暂无用量记录'
        $script:metricsText.Visibility = 'Visible'
        return
    }
    $t = $d.turn
    $u = $t.usage
    $status = switch ($t.status) { 'running' {'本轮'} 'complete' {'最近轮'} 'aborted' {'中断轮'} default {'待确认'} }
    $effort = switch ($d.reasoning_effort) { 'none' {'无'} 'minimal' {'最低'} 'low' {'低'} 'medium' {'中'} 'high' {'高'} 'xhigh' {'极高'} 'max' {'最高'} 'ultra' {'超高'} default { if ($d.reasoning_effort) { $d.reasoning_effort } else { '待记录' } } }
    $script:titleText.Text = $d.name
    $script:modelText.Text = '· ' + $d.model + ' · 思考 ' + $effort
    $script:titleText.ToolTip = $d.name + "`n" + $d.id + "`n模型与思考强度取最近本地日志设置。位于对话区顶部；拖动可微调，点击 ⋯ 恢复默认位置或切换会话。"
    $avg = if ($t.status -eq 'unconfirmed') { $null } else { $t.rate }
    $cost = if ($u -and $t.usage_complete) { Format-USD $t.cost_usd } else { '—' }
    $known = @($d.models | Where-Object {$null -ne $_.cost_usd})
    $total = if ($known.Count) { Format-USD (($known | Measure-Object cost_usd -Sum).Sum) } else { '待定价' }
    if ($known.Count -lt @($d.models).Count) { $total += '*' }
    $script:recentValue.Text = if ($null -eq $t.recent_rate) { if ($t.status -eq 'running') { '等待采样' } else { '暂无采样' } } else { Format-Rate $t.recent_rate }
    $script:recentValue.FontSize = if ($null -eq $t.recent_rate) { 12 } else { 15 }
    $script:recentUnit.Text = if ($null -eq $t.recent_rate) { '' } else { ' t/s' }
    $script:averageValue.Text = Format-Rate $avg
    $script:roundLabel.Text = $status + ' '
    $script:roundValue.Text = $cost
    $script:totalValue.Text = $total
    $script:metricsText.Visibility = 'Collapsed'
}
$menuButton.Add_Click({
    $menu = [Windows.Controls.ContextMenu]::new()
    $detail = [Windows.Controls.MenuItem]::new(); $detail.Header = '在浏览器打开绑定会话详情'
    $detail.Add_Click({ Start-Process ('http://127.0.0.1:8767/?thread='+$script:config.thread+'&mode=details') })
    [void]$menu.Items.Add($detail)
    $reset = [Windows.Controls.MenuItem]::new(); $reset.Header = '恢复对话区顶部位置'
    $reset.Add_Click({ $script:config.offsetX = 0; $script:config.offsetY = 0; Save-FloatingConfig })
    [void]$menu.Items.Add($reset)
    [void]$menu.Items.Add([Windows.Controls.Separator]::new())
    foreach ($row in $script:threads) {
        $item = [Windows.Controls.MenuItem]::new(); $item.Header = $row.name; $item.Tag = $row.id
        $item.IsCheckable = $true; $item.IsChecked = $row.id -eq $script:config.thread
        $item.Add_Click({ param($sender,$event) $script:config.thread = $sender.Tag; Save-FloatingConfig; $script:lastRequest = [datetime]::MinValue; $script:titleText.Text = '正在切换绑定会话'; $script:modelText.Text = ''; $script:metricsText.Text = '等待新会话采样'; $script:metricsText.Visibility = 'Visible' })
        [void]$menu.Items.Add($item)
    }
    $script:menuButton.ContextMenu = $menu
    $menu.PlacementTarget = $script:menuButton; $menu.IsOpen = $true
})
$window.FindName('TitleArea').Add_MouseLeftButtonDown({
    $script:dragging = $true
    $script:window.DragMove()
    $script:dragging = $false
    $rect = [TokenFloatNative+Rect]::new(); $ownerRect = [TokenFloatNative+Rect]::new()
    [void][TokenFloatNative]::GetWindowRect($script:handle,[ref]$rect)
    [void][TokenFloatNative]::GetWindowRect($script:ownerHandle,[ref]$ownerRect)
    if ($script:anchor) {
        $scale = [TokenFloatNative]::GetDpiForWindow($script:ownerHandle)/96.0
        $script:config.offsetX = $rect.Left-($script:anchor[0]-8*$scale)
        $script:config.offsetY = $rect.Top-($ownerRect.Top+100*$scale)
    }
    Save-FloatingConfig
})
$timer = [Windows.Threading.DispatcherTimer]::new()
$timer.Interval = [TimeSpan]::FromMilliseconds(250)
$timer.Add_Tick({
    if (-not [TokenFloatNative]::IsWindow($script:ownerHandle)) {
        $app = Get-Process -Name ChatGPT,Codex -ErrorAction SilentlyContinue | Where-Object {$_.MainWindowHandle -ne 0} | Select-Object -First 1
        if ($app) { $script:ownerHandle = [IntPtr]$app.MainWindowHandle; $script:interop.Owner = $script:ownerHandle; $script:anchor = $null }
    }
    if ($script:locateJob -and $script:locateJob.IsCompleted) {
        $box = @($script:locator.EndInvoke($script:locateJob))
        if ($box.Count -eq 4) { $script:anchor = $box; $script:anchorAt = [datetime]::Now }
        $script:locateJob = $null
    }
    $foreground = [TokenFloatNative]::GetForegroundWindow()
    $active = $script:ownerHandle -ne [IntPtr]::Zero -and -not [TokenFloatNative]::IsIconic($script:ownerHandle) -and ($foreground -eq $script:ownerHandle -or $foreground -eq $script:handle -or $script:menuButton.ContextMenu.IsOpen)
    if ($active -and -not $script:locateJob -and ([datetime]::Now-$script:lastLocate).TotalMilliseconds -ge 750) {
        $script:locator.Commands.Clear()
        $script:locator.Streams.Error.Clear()
        [void]$script:locator.AddScript($script:locateScript).AddArgument($script:ownerHandle.ToInt64())
        $script:locateJob = $script:locator.BeginInvoke()
        $script:lastLocate = [datetime]::Now
    }
    if (-not $active -or -not $script:anchor -or ([datetime]::Now-$script:anchorAt).TotalSeconds -gt 3) {
        if ($script:window.IsVisible) { $script:window.Hide() }
    } else {
        if (-not $script:window.IsVisible) { $script:window.Show() }
        if (-not $script:dragging) {
            $scale = [TokenFloatNative]::GetDpiForWindow($script:ownerHandle)/96.0
            $width = [int]$script:anchor[2]
            $height = [int](54*$scale)
            $ownerRect = [TokenFloatNative+Rect]::new(); [void][TokenFloatNative]::GetWindowRect($script:ownerHandle,[ref]$ownerRect)
            $left = [int]($script:anchor[0]-8*$scale+$script:config.offsetX)
            $top = [int]($ownerRect.Top+100*$scale+$script:config.offsetY)
            $script:titleText.MaxWidth = [Math]::Max(50,$width/$scale-$script:modelText.ActualWidth-112)
            [void][TokenFloatNative]::SetWindowPos($script:handle,[IntPtr]::Zero,$left,$top,$width,$height,0x0014)
        }
    }
    if ($script:pending -and $script:pending.IsCompleted) {
        try {
            $data = $script:pending.GetAwaiter().GetResult() | ConvertFrom-Json
            if ($script:requestedThread -eq $script:config.thread) { if (-not $script:config.thread -and $data.selected) { $script:config.thread = $data.selected.id; Save-FloatingConfig }; $script:lastData=$data; $script:threads=@($data.threads); Render-FloatingData $data }
        } catch { $script:metricsText.Text = '连接中断 · 等待本地服务恢复'; $script:metricsText.Visibility = 'Visible' }
        $script:pending = $null
    }
    if (-not $script:pending -and ([datetime]::Now-$script:lastRequest).TotalMilliseconds -ge 500) {
        $script:requestedThread = $script:config.thread
        $script:pending = $script:client.GetStringAsync('http://127.0.0.1:8767/api?thread='+[uri]::EscapeDataString($script:requestedThread))
        $script:lastRequest = [datetime]::Now
    }
})
$window.Add_Closed({ $timer.Stop(); Save-FloatingConfig; if ($script:locator) { $script:locator.Stop(); $script:locator.Dispose() }; $script:client.Dispose(); $mutex.ReleaseMutex(); $mutex.Dispose(); [Windows.Threading.Dispatcher]::CurrentDispatcher.InvokeShutdown() })
$PID | Set-Content (Join-Path $runtime 'floating-status.pid')
$timer.Start()
[Windows.Threading.Dispatcher]::Run()
