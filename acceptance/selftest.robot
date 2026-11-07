# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     The keywords themselves, on the simulated bench: no hardware.
Library           OperatingSystem
Library           oat.robot_library.OpenRTXAcceptance
Resource          keyed.resource
Suite Setup       Run Keywords
...               Open Bench    bench/sim.yaml    calibration/sim.yaml    radios/c62.yaml    sim    simulated radio    dsp=sim
...               AND    Use Band    70cm
Suite Teardown    Close Bench

*** Test Cases ***
Capture Measures Simulated Deviation
    Simulate Radio Transmissions    2000    4500
    ${tx}=    Capture Radio Transmissions    Key up twice    expected=2
    Should Be True    1900 < ${tx}[0][peak_deviation_hz] < 2100
    Should Be True    4400 < ${tx}[1][peak_deviation_hz] < 4600
    Should Be True    990 < ${tx}[1][tone_hz] < 1010
    FM Transmission Should Meet Limits    ${tx}[0]    BW_12_5
    FM Transmission Should Meet Limits    ${tx}[1]    BW_25

Over-Deviation Fails The Narrow Limits
    Simulate Radio Transmissions    4500
    ${tx}=    Capture Radio Transmissions    Key up once
    Run Keyword And Expect Error    BW_12_5: peak_deviation_hz*
    ...    FM Transmission Should Meet Limits    ${tx}[0]    BW_12_5

Missing Transmission Fails The Capture
    Run Keyword And Expect Error    Expected 1 transmission(s), found 0
    ...    Capture Radio Transmissions    Do nothing

Ratio Check
    ${r}=    Ratio Should Be Between    2000    4000    0.4    0.6
    Should Be Equal As Numbers    ${r}    0.5
    Run Keyword And Expect Error    *not in*    Ratio Should Be Between    1    1    0.4    0.6

Transmitter And Operator Run
    Start Transmitting FM Tone    3000
    Operator Confirms    The tone is clean
    Stop Transmitting
    ${tone}=    Create Audio Tone    1000    1
    File Should Exist    ${tone}

CTCSS Is Measured And Checked
    Simulate Radio Transmissions    0:100:400    0:100:700
    ${tx}=    Capture Radio Transmissions    Key up twice with CTCSS    expected=2
    Should Be True    99.9 < ${tx}[0][ctcss_hz] < 100.1
    FM Transmission CTCSS Should Meet Limits    ${tx}[0]    BW_12_5    100
    FM Transmission CTCSS Should Meet Limits    ${tx}[1]    BW_25    100
    Run Keyword And Expect Error    BW_25: ctcss_deviation_hz*
    ...    FM Transmission CTCSS Should Meet Limits    ${tx}[0]    BW_25    100
    Run Keyword And Expect Error    BW_12_5: ctcss_hz*
    ...    FM Transmission CTCSS Should Meet Limits    ${tx}[0]    BW_12_5    103.5

Power Steps Are Checked And Clipping Fails
    Simulate Radio Transmissions    2500@-12    2500@-8    2500@-4
    ${tx}=    Capture Radio Transmissions    Key up at three settings    expected=3
    TX Power Should Rise With Setting    ${{["1 W", "2.5 W", "5 W"]}}    ${tx}
    Simulate Radio Transmissions    2500@-12    2500@-11.5
    ${flat}=    Capture Radio Transmissions    Key up at two settings    expected=2
    Run Keyword And Expect Error    *only +0.5 dB over*
    ...    TX Power Should Rise With Setting    ${{["Low", "High"]}}    ${flat}
    Simulate Radio Transmissions    2500@-6    2500@6
    ${clip}=    Capture Radio Transmissions    Key up at two settings    expected=2
    Run Keyword And Expect Error    *receiver clipped*
    ...    TX Power Should Rise With Setting    ${{["Low", "High"]}}    ${clip}

Power Settings Come From The Radio File Or The Command Line
    ${settings}=    Power Settings
    Should Be Equal    ${settings}    ${{["1 W", "2.5 W", "5 W"]}}
    Set Test Variable    ${POWER_SETTINGS}    Low, High
    ${stock}=    Power Settings
    Should Be Equal    ${stock}    ${{["Low", "High"]}}

Interferer Transmits
    Start Transmitting With Interferer    1500    12500    30    2500    interferer=off
    Operator Confirms    The wanted tone alone
    Stop Transmitting
    Start Transmitting With Interferer    1500    12500    30    2500
    Operator Confirms    Only the wanted tone
    Stop Transmitting

Bands Set The Frequency
    Use Band    2m
    Should Be Equal    ${FREQUENCY_MHZ}    146.580
    Should Be Equal    ${BAND}    2m
    Run Keyword And Expect Error    *has no band '23cm'*    Use Band    23cm
    [Teardown]    Use Band    70cm

Results Are Remembered Per Band
    Remember Result    wide tone    ${42}
    ${value}=    Recall Result    wide tone
    Should Be Equal    ${value}    ${42}
    Use Band    2m
    ${other}=    Recall Result    wide tone
    Should Be Equal    ${other}    ${NONE}
    [Teardown]    Use Band    70cm

Absolute Levels Come From The Calibration
    Simulate Radio Transmissions    2500
    ${tx}=    Capture Radio Transmissions    Key up once
    ${dbm}=    Radio Power In dBm    ${tx}[0]
    Should Be True    33 < ${dbm} < 35
    Radio Power Should Be Between    ${tx}[0]    30    37
    ${setting}=    Transmit Level    -100
    Should Be Equal    ${setting}    ${{(20, 0.0)}}
    ${low}=    Transmit Level    -125
    Should Be Equal As Numbers    ${low}[1]    5
    Run Keyword And Expect Error    *step attenuator*    Transmit Level    -140
    Start Transmitting FM Tone    3000    level_dbm=-110
    Stop Transmitting

M17 Transmissions Are Measured And Decoded
    Simulate Radio M17 Transmissions    2    source=K0RET    frames=10
    ${tx}=    Capture Radio M17 Transmissions    Key up twice in M17    expected=2
    Should Be True    2350 < ${tx}[0][outer_deviation_hz] < 2450
    Should Be Equal    ${tx}[1][source]    K0RET
    M17 Transmission Should Meet Limits    ${tx}[0]
    M17 Transmissions Should Decode    ${tx}    K0RET
    Run Keyword And Expect Error    *not 'N0CALL'*
    ...    M17 Transmissions Should Decode    ${tx}    N0CALL

Bad M17 Fails Its Limits
    Simulate Radio M17 Transmissions    deviation_hz=2000    inner_deviation_hz=800    inverted=True
    ${tx}=    Capture Radio M17 Transmissions    Key up once in M17
    Should Be Equal    ${tx}[0][polarity]    inverted
    Run Keyword And Expect Error    M17: outer_deviation_hz*deviation_ratio*polarity*
    ...    M17 Transmission Should Meet Limits    ${tx}[0]    deviation,ratio,polarity
    M17 Transmission Should Meet Limits    ${tx}[0]    symbol_rate, evm

M17 Stream Transmits
    Start Transmitting M17 Stream    source=K0RET    audio=random    frames=5
    Stop Transmitting

DSP Image Is Mandatory For Radios That Have One
    Run Keyword And Expect Error    *Pass -v DSP:...;*
    ...    Open Bench    bench/sim.yaml    calibration/sim.yaml    radios/c62.yaml    sim    simulated radio

The Bench Keys Each Transmission
    ${keys}=    Bench Keys The Radio
    Should Be True    ${keys}
    Inject Audio Tone    1000    -20
    ${tx}=    Capture Keyed FM Transmissions    2    3
    Stop Injecting Audio
    Length Should Be    ${tx}    3
    Should Be True    ${tx}[0][tone_deviation_hz] > 0

Audio Is Injected And Measured
    Inject Audio Tone    1000    -12    duration_s=1
    ${a}=    Capture Radio Audio    2
    Should Be True    -12.2 < ${a}[level_dbfs] < -11.8
    Should Be True    ${a}[level_mvrms] > 200
    Audio SINAD Should Be At Least    ${a}    40
    Stop Injecting Audio
    ${speech}=    Create Speech-Like Audio    2
    Inject Audio File    ${speech}    gain_db=-6
    Inject Audio Tones    500,1000,2000    -20    tone_s=0.5    loop=True
    ${sweep}=    Capture Radio Audio    4
    ${levels}=    Measure Audio Response    ${sweep}
    Should Be True    abs(${levels}[0][level_db]) < 0.2
    Stop Injecting Audio
    Key Radio
    Release Radio

Simulated Radio Plays What It Receives
    Start Transmitting FM Tone    3000    level_dbm=-80
    ${a}=    Capture Radio Audio    2    1000    psophometric
    Should Be True    990 < ${a}[tone_hz] < 1010
    Audio Distortion Should Meet Limits    ${a}    rx
    Audio Should Have No Dropouts    ${a}
    Stop Transmitting
    ${s}=    Find RX Sensitivity    3000    start_dbm=-110    stop_dbm=-126    step_db=2
    Should Be True    -124 < ${s}[sensitivity_dbm] < -115

Transmitted Audio Is Measured
    Simulate Radio Transmissions    3000
    ${tx}=    Capture Radio Transmissions    Key up with a tone
    ${a}=    Measure Transmitted Audio    ${tx}[0]
    Should Be True    2950 < ${a}[tone_amplitude] < 3050
    ${m}=    Mic Sensitivity    ${tx}[0]    -30    1500
    Should Be True    -36.1 < ${m}[sensitivity_dbfs] < -35.9
    Spread Should Be At Most    deviation    1.0    3000    3050    2980
    Run Keyword And Expect Error    *spreads over*    Spread Should Be At Most    deviation    1.0    3000    3500
