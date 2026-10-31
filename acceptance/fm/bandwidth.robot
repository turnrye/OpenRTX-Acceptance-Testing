# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     FM follows the 12.5/25 kHz channel bandwidth setting, on transmit
...               and on receive, on every band the radio has.
...
...               Run it with a bench's argument file (bench/example.args), whose
...               oat.expand repeats each test on every band in the radio file. Transmit is measured from the SDR capture; receive is
...               judged by the operator, until the radio can report its own audio.
...               The programming cable stays out throughout: it disconnects the mic
...               and the speaker.
...
...               To measure stock firmware as a reference, exclude mode:m17.
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}
Suite Teardown    Close Bench
Test Tags         per-band    mode:fm

*** Variables ***
# Per tester and per run, so no defaults: pass them with -v NAME:value
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
# Narrow over wide peak deviation for the same tone at the mic: the BK4819's
# 12.5 kHz mode halves it (0.47-0.60 measured on a C2, 2026-10-30; the spread
# is the acoustic tone moving between takes)
${RATIO_LOW}      0.4
${RATIO_HIGH}     0.65
${CTCSS_HZ}       100.0
# Interferer level over the wanted signal. A check that a neighbour doesn't
# break through, not a selectivity figure: at 30 dB a HackRF hits its gain
# limit and leaves the wanted signal near the radio's threshold (C2, 2026-10-30)
${ADJACENT_DB}    20

*** Test Cases ***
FM TX At 25 kHz Stays Within Wide Limits
    [Tags]    tx
    ${tone}=    Create Audio Tone    1000
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, low power, no CTCSS, programming cable out
    ${tx}=    Capture Radio Transmissions
    ...    Play ${tone} at a fixed volume and distance from the mic, key PTT for 5 s, release. Then key PTT, whistle loudly close to the mic for 5 s, release.
    ...    expected=2
    FM Transmission Should Meet Limits    ${tx}[0]    BW_25
    FM Transmission Should Meet Limits    ${tx}[1]    BW_25
    Remember Result    wide tone    ${tx}[0]

FM TX At 12.5 kHz Stays Within Narrow Limits
    [Tags]    tx
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, low power, no CTCSS
    ${tx}=    Capture Radio Transmissions
    ...    Play the same tone at the same volume and distance, key PTT for 5 s, release. Then key PTT, whistle loudly close to the mic for 5 s, release.
    ...    expected=2
    FM Transmission Should Meet Limits    ${tx}[0]    BW_12_5
    FM Transmission Should Meet Limits    ${tx}[1]    BW_12_5
    Remember Result    narrow tone    ${tx}[0]

Narrow FM TX Deviates Less Than Wide
    [Tags]    tx
    ${wide}=    Recall Result    wide tone
    ${narrow}=    Recall Result    narrow tone
    Skip If    $wide is None or $narrow is None
    ...    Needs the tone takes of both bandwidth tests on this band
    Ratio Should Be Between    ${narrow}[peak_deviation_hz]    ${wide}[peak_deviation_hz]
    ...    ${RATIO_LOW}    ${RATIO_HIGH}    narrow/wide peak deviation

FM RX At 25 kHz Is Clean Up To Full Deviation
    [Tags]    rx    manual
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, no CTCSS, volume comfortable
    Start Transmitting FM Tone    3000
    Operator Confirms    The radio plays a clean, steady 1 kHz tone
    Stop Transmitting
    Start Transmitting FM Tone    5000
    Operator Confirms    The radio plays the tone louder and still undistorted
    [Teardown]    Stop Transmitting

FM RX At 12.5 kHz Is Clean Up To Full Deviation
    [Tags]    rx    manual
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, no CTCSS, volume unchanged
    Start Transmitting FM Tone    1500
    Operator Confirms    The radio plays a clean, steady 1 kHz tone
    Stop Transmitting
    Start Transmitting FM Tone    2500
    Operator Confirms    The radio plays the tone louder and still undistorted
    Operator Reports    Compared with the louder tone at 25 kHz, is this quieter, the same, or louder?
    [Teardown]    Stop Transmitting

FM Bandwidth Survives An M17 Round Trip
    [Tags]    tx    mode:m17
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, low power, then switch to M17 and back to FM
    ${tx}=    Capture Radio Transmissions
    ...    Play the same tone at the same volume and distance, key PTT for 5 s, release.
    FM Transmission Should Meet Limits    ${tx}[0]    BW_12_5
    ${narrow}=    Recall Result    narrow tone
    Skip If    $narrow is None    Needs the 12.5 kHz tone take on this band to compare with
    Ratio Should Be Between    ${tx}[0][peak_deviation_hz]    ${narrow}[peak_deviation_hz]
    ...    0.8    1.25    deviation after M17 / before

CTCSS Deviation Follows The Bandwidth
    [Tags]    tx    ctcss
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, low power, ${CTCSS_HZ} Hz CTCSS tone on transmit
    ${wide}=    Capture Radio Transmissions    Key PTT for 5 s without speaking, release.
    Remember Result    wide ctcss    ${wide}[0]
    Operator Does    Set the bandwidth to 12.5 kHz, CTCSS tone unchanged
    ${narrow}=    Capture Radio Transmissions    Key PTT for 5 s without speaking, release.
    Remember Result    narrow ctcss    ${narrow}[0]
    Ratio Should Be Between    ${narrow}[0][ctcss_deviation_hz]    ${wide}[0][ctcss_deviation_hz]
    ...    ${RATIO_LOW}    ${RATIO_HIGH}    narrow/wide CTCSS deviation
    [Teardown]    Operator Does    Turn the CTCSS tone off

CTCSS Level Meets Limits
    [Tags]    tx    ctcss
    ${wide}=    Recall Result    wide ctcss
    ${narrow}=    Recall Result    narrow ctcss
    Skip If    $wide is None or $narrow is None
    ...    Needs both CTCSS takes of the previous test on this band
    Run Keyword And Continue On Failure
    ...    FM Transmission CTCSS Should Meet Limits    ${wide}    BW_25    ${CTCSS_HZ}
    FM Transmission CTCSS Should Meet Limits    ${narrow}    BW_12_5    ${CTCSS_HZ}

FM TX Power Rises With Each Setting
    [Documentation]    Relative levels through the bench: no power meter needed. For
    ...                stock firmware, name its settings: -v POWER_SETTINGS:Low,High
    [Tags]    tx    power
    ${settings}=    Power Settings
    ${order}=    Evaluate    ", then ".join($settings)
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, no CTCSS, power ${settings}[0]
    ${tx}=    Capture Radio Transmissions
    ...    For each power setting in turn (${order}): set it, key PTT for 3 s without speaking, release.
    ...    expected=${{len($settings)}}
    TX Power Should Rise With Setting    ${settings}    ${tx}
    [Teardown]    Run Keyword If    $TEST_STATUS != 'SKIP'
    ...    Operator Does    Set the power back to ${settings}[0]

FM RX At 12.5 kHz Rejects An Adjacent Channel
    [Tags]    rx    manual    adjacent
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, no CTCSS, volume comfortable
    Start Transmitting With Interferer    1500    12500    ${ADJACENT_DB}    2500    interferer=off
    Operator Confirms    The wanted signal alone: a clean, steady 1 kHz tone
    Stop Transmitting
    Start Transmitting With Interferer    1500    12500    ${ADJACENT_DB}    2500
    Operator Confirms    The 1 kHz tone continues with no 400 Hz tone from the channel above
    Operator Reports    Any added noise compared with the wanted signal alone?
    Stop Transmitting
    Start Transmitting With Interferer    1500    -12500    ${ADJACENT_DB}    2500
    Operator Confirms    With the interferer on the channel below: still no 400 Hz tone
    [Teardown]    Stop Transmitting

FM RX At 25 kHz Rejects An Adjacent Channel
    [Tags]    rx    manual    adjacent
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, no CTCSS, volume comfortable
    Start Transmitting With Interferer    3000    25000    ${ADJACENT_DB}    5000    interferer=off
    Operator Confirms    The wanted signal alone: a clean, steady 1 kHz tone
    Stop Transmitting
    Start Transmitting With Interferer    3000    25000    ${ADJACENT_DB}    5000
    Operator Confirms    The 1 kHz tone continues with no 400 Hz tone from the channel above
    Operator Reports    Any added noise compared with the wanted signal alone?
    Stop Transmitting
    # Not a requirement: shows what the wide filter lets through from a narrow neighbour
    Start Transmitting With Interferer    3000    12500    ${ADJACENT_DB}    2500
    Operator Reports    With a 12.5 kHz neighbour this close, do you hear the 1 kHz tone only, both tones, or mostly 400 Hz?
    [Teardown]    Stop Transmitting
