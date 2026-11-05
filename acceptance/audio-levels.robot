# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     Setting up an audio interface, not evidence: keep these runs local.
...               Run it on a new interface, cable or mixer setting, before the suites
...               that use it, and read the table it prints. A key-up that fails means
...               the PTT line or port is wrong.
...
...               Injected tones step up in level. Deviation should rise with the
...               level, 5 dB per step, until the limiter holds it, and THD+N should
...               stay low: the clean range is below that point. The mic sensitivity
...               test's probe level (-40 dBFS in fm/audio.robot) must be inside it,
...               so change the mixer if it isn't. The hum and noise figures should
...               not rise with the interface plugged into a ground loop: compare
...               them with and without an isolator.
Library           Collections
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
Suite Teardown    Close Bench
Test Tags         per-band    mode:fm    needs:audio    tx

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
${DSP}            ${EMPTY}
@{LEVELS_DBFS}    -50    -45    -40    -35    -30    -25    -20    -15    -10
${KEYED_S}        3

*** Keywords ***
Capture Keyed Transmission
    [Documentation]    One transmission of ${KEYED_S} s, keyed through Key Radio, with a
    ...                second of silence on each side; returns its measurements.
    Start Capture
    Wait Seconds    1
    Key Radio
    Wait Seconds    ${KEYED_S}
    Release Radio
    Wait Seconds    1
    ${tx}=    Stop Capture And Measure FM
    RETURN    ${tx}[0]

*** Test Cases ***
Mic Level Window
    [Documentation]    Deviation and THD+N of a 1 kHz tone at each injected level.
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, low power, no CTCSS
    ${header}=    Evaluate    f"{'level dBFS':>10} {'deviation Hz':>14} {'THD+N %':>9}"
    Log To Console    \n${header}
    FOR    ${level}    IN    @{LEVELS_DBFS}
        Inject Audio Tone    1000    ${level}
        ${tx}=    Capture Keyed Transmission
        Stop Injecting Audio
        ${audio}=    Measure Transmitted Audio    ${tx}    1000
        ${row}=    Evaluate    f"{float($level):10.0f} {$tx['tone_deviation_hz']:14.0f} {$audio['thd_n_percent']:9.1f}"
        Log To Console    ${row}
        Set Test Message    ${row}${\n}    append=True
    END
    [Teardown]    Stop Injecting Audio

Hum And Noise With The Mic Input Silent
    [Documentation]    Residual deviation on a key-up with nothing injected, and the level
    ...                from the speaker output with the radio squelched: hum here points to
    ...                a ground loop through the interface.
    Operator Does    Keep the radio on FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, low power, squelched, with no signal on the frequency
    ${tx}=    Capture Keyed Transmission
    ${speaker}=    Capture Radio Audio    3
    ${row}=    Evaluate    f"residual deviation {$tx['rms_deviation_hz']:.1f} Hz RMS, speaker {$speaker['level_dbfs']:.1f} dBFS"
    Log To Console    \n ${row}
    Set Test Message    ${row}
