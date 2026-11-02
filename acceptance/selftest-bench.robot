# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     The bench measures itself: the transmitter plays known signals
...               through the attenuator chain into the receiver, and the analysis
...               must read back what was sent. Run it at the start of every evidence
...               session; its spread over five repeats is the repeatability that
...               comparisons between firmware states are judged against.
...
...               No radio is connected. The transmitter and the receiver must be
...               separate instruments, such as a HackRF transmitting and an RTL-SDR
...               receiving: a HackRF can't do both at once. FIRMWARE and HARDWARE
...               still name the radio the session is for. Each band of the radio
...               file is checked.
...
...               On the simulated bench (bench/sim.yaml) the simulated transmitter
...               loops back into the simulated receiver, so CI runs it too.
Library           Collections
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Run Keywords
...               Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
...               AND    Require Separate Instruments
...               AND    Operator Does    Disconnect the radio. Connect the transmitter's output through the attenuator chain to the receiver's input.
Suite Teardown    Close Bench
Test Tags         per-band    bench

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
${DSP}            ${EMPTY}
# First zero of the Bessel function J0, times a 1 kHz tone: the carrier
# vanishes at this deviation, whatever any instrument's scale says
${BESSEL_NULL_HZ}    2404.83
# How deep the carrier must fall there, relative to the whole signal: a
# deviation off by 1% leaves it near -35 dB
${MAX_NULL_DB}    -30
# Agreement between the deviation the analysis reads and the physical one
${DEVIATION_TOLERANCE}    0.01
${REPEATS}        5
${KEYED_S}        3
${M17_FRAMES}     50

*** Keywords ***
Loop Back
    [Documentation]    Capture what the transmitter keyword sends, keyed for ${KEYED_S} s
    ...                between a second of silence on each side.
    [Arguments]    ${start keyword}    @{args}    &{kwargs}
    Start Capture
    Wait Seconds    1
    Run Keyword    ${start keyword}    @{args}    &{kwargs}
    Wait Seconds    ${KEYED_S}
    Stop Transmitting

Loop Back FM
    [Arguments]    @{args}    &{kwargs}
    Loop Back    Start Transmitting FM Tone    @{args}    &{kwargs}
    Wait Seconds    1
    ${tx}=    Stop Capture And Measure FM
    RETURN    ${tx}[0]

Loop Back M17
    [Documentation]    One M17 transmission of ${M17_FRAMES} frames from OAT1, played once.
    Start Capture
    Wait Seconds    1
    Start Transmitting M17 Stream    source=OAT1    audio=random    frames=${M17_FRAMES}
    ...    gap_s=0    loop=False
    Wait Seconds    ${{${M17_FRAMES} * 0.04 + 0.3}}
    Stop Transmitting
    Wait Seconds    1
    ${tx}=    Stop Capture And Measure M17
    RETURN    ${tx}[0]

*** Test Cases ***
Deviation Scale Holds At The Bessel Null
    ${tx}=    Loop Back FM    ${BESSEL_NULL_HZ}    1000
    Value Should Be At Most    ${tx}[carrier_level_db]    ${MAX_NULL_DB}    carrier at the Bessel null (dB)
    Value Should Be Within    ${tx}[tone_deviation_hz]    ${BESSEL_NULL_HZ}    ${DEVIATION_TOLERANCE}
    ...    deviation read at the Bessel null (Hz)

Known FM Deviation Reads Back
    ${tx}=    Loop Back FM    3000    1000
    Value Should Be Within    ${tx}[tone_deviation_hz]    3000    ${DEVIATION_TOLERANCE}    tone deviation (Hz)
    Value Should Be Within    ${tx}[peak_deviation_hz]    3000    0.03    peak deviation (Hz)
    Value Should Be Within    ${tx}[rms_deviation_hz]    ${{3000 / 2 ** 0.5}}    0.03    RMS deviation (Hz)

Known M17 Stream Reads Back
    [Tags]    mode:m17
    ${tx}=    Loop Back M17
    Value Should Be Within    ${tx}[outer_deviation_hz]    2400    ${DEVIATION_TOLERANCE}    M17 outer deviation (Hz)
    Value Should Be Within    ${tx}[deviation_ratio]    3    0.02    M17 deviation ratio
    M17 Transmissions Should Decode    ${{[$tx]}}    OAT1

FM Readings Repeat
    [Documentation]    ${REPEATS} loopbacks of the FM tone, at 3 kHz and at the Bessel null:
    ...                the spread of each reading is the bench's repeatability.
    ${fm}=    Create List
    ${null}=    Create List
    FOR    ${i}    IN RANGE    ${REPEATS}
        ${tx}=    Loop Back FM    3000    1000
        Append To List    ${fm}    ${tx}[tone_deviation_hz]
        ${tx}=    Loop Back FM    ${BESSEL_NULL_HZ}    1000
        Append To List    ${null}    ${tx}[carrier_level_db]
    END
    Report Repeatability    FM tone deviation (Hz)    @{fm}
    Report Repeatability    carrier at the Bessel null (dB)    @{null}

M17 Readings Repeat
    [Documentation]    ${REPEATS} loopbacks of the M17 stream.
    [Tags]    mode:m17
    ${m17}=    Create List
    ${ratio}=    Create List
    FOR    ${i}    IN RANGE    ${REPEATS}
        ${tx}=    Loop Back M17
        Append To List    ${m17}    ${tx}[outer_deviation_hz]
        Append To List    ${ratio}    ${tx}[deviation_ratio]
    END
    Report Repeatability    M17 outer deviation (Hz)    @{m17}
    Report Repeatability    M17 deviation ratio    @{ratio}
