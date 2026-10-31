# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     The keywords themselves, on the simulated bench: no hardware.
Library           OperatingSystem
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Run Keywords
...               Open Bench    bench/sim.yaml    calibration/sim.yaml    radios/c62.yaml    sim
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
