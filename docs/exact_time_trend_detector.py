//@version=5
indicator("Exact Time Trend Detector", overlay=true, max_labels_count=500)

// ==========================================
// 1. INPUTS: DATE/TIME & INDICATOR SETTINGS
// ==========================================
// Target Timestamp Input (Defaults to current time; change via settings)
i_targetTime = input.time(timestamp("2026-11-08 11:15"), title="Target Date & Time", group="Time Selection")

// Trend Indicator Settings
i_emaFastPeriod = input.int(9, title="Fast EMA Period", group="Indicator Settings")
i_emaSlowPeriod = input.int(21, title="Slow EMA Period", group="Indicator Settings")
i_stPeriod      = input.int(10, title="Supertrend ATR Period", group="Indicator Settings")
i_stFactor      = input.float(3.0, title="Supertrend Multiplier", group="Indicator Settings")
i_adxPeriod     = input.int(14, title="ADX Period", group="Indicator Settings")

// Table Position Settings
i_tableLoc      = input.string("Top Right", title="Dashboard Position", 
                   options=["Top Right", "Bottom Right", "Top Left", "Bottom Left"], group="Visuals")

// ==========================================
// 2. TREND CALCULATIONS
// ==========================================
// EMA Trend & Slope
emaFast = ta.ema(close, i_emaFastPeriod)
emaSlow = ta.ema(close, i_emaSlowPeriod)
isEmaBull = emaFast > emaSlow and emaFast > emaFast[1]
isEmaBear = emaFast < emaSlow and emaFast < emaFast[1]

// Supertrend Direction
[stValue, stDirection] = ta.supertrend(i_stFactor, i_stPeriod)
isStBull = stDirection < 0  // 0 means bullish in TradingView Pine Script
isStBear = stDirection > 0

// ADX Strength Calculation
[plusDI, minusDI, adxValue] = ta.dmi(i_adxPeriod, i_adxPeriod)
isStrongTrend = adxValue >= 20

// Overall Trend Matrix Logic
string overallTrend = "NEUTRAL / SIDEWAYS ↔️"
color trendColor   = color.gray

if isEmaBull and isStBull
    if isStrongTrend
        overallTrend := "STRONG UPTREND 🚀"
        trendColor   := color.green
    else
        overallTrend := "WEAK UPTREND ↗️"
        trendColor   := color.teal
else if isEmaBear and isStBear
    if isStrongTrend
        overallTrend := "STRONG DOWNTREND 📉"
        trendColor   := color.red
    else
        overallTrend := "WEAK DOWNTREND ↘️"
        trendColor   := color.maroon

// Plotting moving averages on chart for reference
plot(emaFast, title="Fast EMA", color=color.new(color.blue, 20), linewidth=2)
plot(emaSlow, title="Slow EMA", color=color.new(color.orange, 20), linewidth=2)

// ==========================================
// 3. TARGET TIMESTAMP MATCHING & PLOTTING
// ==========================================
// Match target timestamp to bar time (within bar duration interval)
bool isTargetBar = (time <= i_targetTime) and (time + timeframe.multiplier * 60000 > i_targetTime)

// Plot chart label at exact bar match
if isTargetBar
    label.new(
       x=bar_index, 
       y=high, 
       text="📍 [" + str.format_time(time, "yyyy-MM-dd HH:mm", syminfo.timezone) + "]\n" + overallTrend + "\nADX: " + str.tostring(adxValue, "#.#"), 
       color=trendColor, 
       textcolor=color.white, 
       style=label.style_label_down, 
       size=size.normal
       )

// ==========================================
// 4. PERSISTENT DASHBOARD TABLE
// ==========================================
// Determine table position enum
string tablePos = switch i_tableLoc
    "Top Right"    => position.top_right
    "Bottom Right" => position.bottom_right
    "Top Left"     => position.top_left
    "Bottom Left"  => position.bottom_left

var table statusTable = table.new(position=tablePos, columns=2, rows=5, bgcolor=color.new(color.black, 20), border_width=1, border_color=color.gray)

if barstate.islast
    // Header
    table.cell(statusTable, 0, 0, "TREND METRIC", bgcolor=color.navy, text_color=color.white, text_size=size.small)
    table.cell(statusTable, 1, 0, "STATUS AT POINT", bgcolor=color.navy, text_color=color.white, text_size=size.small)
    
    // Rows
    table.cell(statusTable, 0, 1, "Overall Bias", text_color=color.white, text_size=size.small)
    table.cell(statusTable, 1, 1, overallTrend, bgcolor=trendColor, text_color=color.white, text_size=size.small)
    
    table.cell(statusTable, 0, 2, "9/21 EMA Alignment", text_color=color.white, text_size=size.small)
    table.cell(statusTable, 1, 2, isEmaBull ? "Bullish (Fast > Slow)" : isEmaBear ? "Bearish (Fast < Slow)" : "Neutral", text_color=isEmaBull ? color.green : isEmaBear ? color.red : color.gray, text_size=size.small)
    
    table.cell(statusTable, 0, 3, "Supertrend Mode", text_color=color.white, text_size=size.small)
    table.cell(statusTable, 1, 3, isStBull ? "Bullish (Price Above)" : "Bearish (Price Below)", text_color=isStBull ? color.green : color.red, text_size=size.small)
    
    table.cell(statusTable, 0, 4, "ADX Trend Strength", text_color=color.white, text_size=size.small)
    table.cell(statusTable, 1, 4, str.tostring(adxValue, "#.#") + (isStrongTrend ? " (Trending)" : " (Weak/Ranging)"), text_color=isStrongTrend ? color.lime : color.orange, text_size=size.small)