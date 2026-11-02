# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     FM audio, on transmit and on receive, measured electrically: test
...               audio goes into the mic input and the speaker is recorded through a
...               USB sound interface on the radio's accessory connector (the bench
...               file's audio section). The connector must mute the internal mic
...               and speaker while plugged; check that once per radio.
...
...               Every test needs the audio interface (needs:audio); the receive
...               sensitivity also needs absolute transmitter levels
...               (needs:abs-level). Key-ups go through the interface's PTT line
...               when the bench has one, otherwise the operator keys on request.
...               Run it on every firmware state to compare: stock firmware
...               included, with -v FIRMWARE:stock-... and -v DSP:stock.
Library           Collections
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
Suite Teardown    Close Bench
Test Tags         per-band    mode:fm    needs:audio

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
${DSP}            ${EMPTY}
# First take of the mic sensitivity: low enough to stay below the limiter
${PROBE_DBFS}     -40
# The response sweep runs this far below the sensitivity level, so
# pre-emphasis at 3 kHz doesn't reach the limiter
${SWEEP_BELOW_DB}    10
${RESPONSE_TONES}    300,500,1000,2000,3000
# A speech recording for the speech-band deviation (FM-TX-4), the same file
# on every firmware state; without one, a repeatable speech-shaped signal
${SPEECH_WAV}     ${EMPTY}
${COLD_BOOTS}     5
${KEYED_S}        5

*** Keywords ***
Capture Keyed Transmission
    [Documentation]    One transmission of ${seconds} s, keyed through Key Radio, with a
    ...                second of silence on each side; returns its measurements.
    [Arguments]    ${seconds}=${KEYED_S}
    Start Capture
    Wait Seconds    1
    Key Radio
    Wait Seconds    ${seconds}
    Release Radio
    Wait Seconds    1
    ${tx}=    Stop Capture And Measure FM
    RETURN    ${tx}[0]

Sensitivity Level
    [Documentation]    The injected level for 60% of peak deviation found by FM-TX-1 on
    ...                this band, or the probe level when it hasn't run.
    ${s}=    Recall Result    mic sensitivity
    IF    $s is None    RETURN    ${PROBE_DBFS}
    RETURN    ${s}[sensitivity_dbfs]

*** Test Cases ***
FM TX Mic Sensitivity
    [Documentation]    FM-TX-1: the 1 kHz level at the mic input that deviates 60% of the
    ...                25 kHz peak limit, in dBFS of the interface and mVrms when calibrated.
    [Tags]    tx
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, low power, no CTCSS, audio interface plugged in
    ${target}=    Test Deviation    BW_25
    Inject Audio Tone    1000    ${PROBE_DBFS}
    ${tx}=    Capture Keyed Transmission
    ${s}=    Mic Sensitivity    ${tx}    ${PROBE_DBFS}    ${target}
    Remember Result    mic sensitivity    ${s}
    # The scaled level must give the target deviation within 1 dB
    Inject Audio Tone    1000    ${s}[sensitivity_dbfs]
    ${check}=    Capture Keyed Transmission
    Value Should Be Within    ${check}[tone_deviation_hz]    ${target}    0.12
    ...    deviation at the sensitivity level (Hz)
    [Teardown]    Stop Injecting Audio

FM TX Audio Response Follows Pre-emphasis
    [Documentation]    FM-TX-2: tones from 300 to 3000 Hz at one level, demodulated, relative
    ...                to 1 kHz.
    [Tags]    tx
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, low power, no CTCSS
    ${level}=    Sensitivity Level
    Inject Audio Tones    ${RESPONSE_TONES}    ${{${level} - ${SWEEP_BELOW_DB}}}    loop=True
    ${tx}=    Capture Keyed Transmission    8
    ${response}=    Measure Audio Response    ${tx}
    Remember Result    tx response    ${response}
    Audio Response Should Meet Limits    ${response}    tx
    [Teardown]    Stop Injecting Audio

FM TX Distortion And Hum And Noise
    [Documentation]    FM-TX-3: THD+N of the demodulated 1 kHz tone at the sensitivity
    ...                level, and the residual deviation with the mic input silent, relative
    ...                to that tone.
    [Tags]    tx
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, low power, no CTCSS
    ${level}=    Sensitivity Level
    Inject Audio Tone    1000    ${level}
    ${tone}=    Capture Keyed Transmission
    ${audio}=    Measure Transmitted Audio    ${tone}    1000
    Audio Distortion Should Meet Limits    ${audio}    tx
    Stop Injecting Audio
    ${quiet}=    Capture Keyed Transmission
    Report Ratio In dB    hum and noise    ${quiet}[rms_deviation_hz]    ${tone}[rms_deviation_hz]
    [Teardown]    Stop Injecting Audio

FM TX Speech Deviation
    [Documentation]    FM-TX-4: RMS and peak deviation of the same speech, injected at the
    ...                same level, so firmware states compare like for like.
    [Tags]    tx
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, low power, no CTCSS
    ${speech}=    Set Variable If    $SPEECH_WAV    ${SPEECH_WAV}
    IF    not $speech
        ${speech}=    Create Speech-Like Audio    10
    END
    Inject Audio File    ${speech}    loop=True
    ${tx}=    Capture Keyed Transmission    8
    Set Test Message    RMS deviation ${tx}[rms_deviation_hz] Hz, peak ${tx}[peak_deviation_hz] Hz
    [Teardown]    Stop Injecting Audio

FM TX Mic Level Is The Same After Every Cold Boot
    [Documentation]    FM-TX-5: the deviation of one injected tone after each of
    ...                ${COLD_BOOTS} cold boots; the mic path must come up the same.
    [Tags]    tx    manual
    ${level}=    Sensitivity Level
    ${deviations}=    Create List
    Inject Audio Tone    1000    ${level}
    FOR    ${i}    IN RANGE    ${COLD_BOOTS}
        Operator Does    Turn the radio off with the knob, wait 5 s, turn it on and wait until it has booted. Set FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, low power, no CTCSS if it didn't keep them.
        ${tx}=    Capture Keyed Transmission
        Append To List    ${deviations}    ${tx}[tone_deviation_hz]
    END
    Mic Level Spread Should Meet Limits    @{deviations}
    [Teardown]    Stop Injecting Audio

FM RX 12 dB SINAD Sensitivity
    [Documentation]    FM-RX-1: the level at the antenna port giving 12 dB SINAD
    ...                (psophometric), 1 kHz at the reference deviation, both bandwidths.
    [Tags]    rx    needs:abs-level
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, no CTCSS, squelch open, volume at the fixed test step
    ${narrow}=    Test Deviation    BW_12_5
    ${s12}=    Find RX Sensitivity    ${narrow}
    Remember Result    sensitivity BW_12_5    ${s12}
    Operator Does    Set the bandwidth to 25 kHz, everything else unchanged
    ${wide}=    Test Deviation    BW_25
    ${s25}=    Find RX Sensitivity    ${wide}
    Remember Result    sensitivity BW_25    ${s25}

FM RX Audio Response And Level
    [Documentation]    FM-RX-2: tones from 300 to 3000 Hz at one deviation, recorded from
    ...                the speaker relative to 1 kHz, and the 1 kHz level at the fixed
    ...                volume step.
    [Tags]    rx
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, no CTCSS, volume at the fixed test step
    ${dev}=    Test Deviation    BW_25
    Start Transmitting FM Tones    ${dev}    ${RESPONSE_TONES}
    ${sweep}=    Capture Radio Audio    14
    ${response}=    Measure Audio Response    ${sweep}
    Audio Response Should Meet Limits    ${response}    rx
    Stop Transmitting
    Start Transmitting FM Tone    ${dev}
    ${tone}=    Capture Radio Audio    3    1000
    Audio Distortion Should Meet Limits    ${tone}    rx
    Set Test Message    1 kHz at ${tone}[level_dbfs] dBFS
    [Teardown]    Stop Transmitting

FM RX Is Not Interrupted By Key Presses
    [Documentation]    FM-RX-3: with voice prompts on and key beeps off, key presses during
    ...                reception must not interrupt the speaker audio.
    [Tags]    rx    manual
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, no CTCSS, voice prompts on, key beeps off, volume at the fixed test step
    ${dev}=    Test Deviation    BW_25
    Start Transmitting FM Tone    ${dev}
    Operator Does    When you continue, the speaker is recorded for 8 s: press a few keys (menu, up, down, back) during it, without changing channel
    ${a}=    Capture Radio Audio    8    1000
    Audio Should Have No Dropouts    ${a}
    [Teardown]    Stop Transmitting
