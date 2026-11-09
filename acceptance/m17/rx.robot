# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     M17 on receive: the transmitter plays a generated voice stream
...               (oat.m17: preamble, LSF from ${SOURCE}, stream frames carrying a Codec2
...               tone, EOT, looped with a gap) and the radio must show the callsign
...               and play the tone. With the audio interface the speaker is measured;
...               without it the operator listens.
...
...               Generating the Codec2 tone needs c2enc (the codec2 package).
Library           oat.robot_library.OpenRTXAcceptance
Resource          ../keyed.resource
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
Suite Teardown    Close Bench
Test Tags         per-band    mode:m17    rx

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
${DSP}            ${EMPTY}
${SOURCE}         OAT1
${TONE}           tone:1000
# Lowest SINAD of the decoded tone that counts as audio present (M17-RX-2)
${MIN_SINAD_DB}    10
@{OFFSETS_HZ}     -2000    -1000    -500    500    1000    2000
# How far the decoded tone's level may move after the radio transmits:
# about the repeatability of a speaker capture
${TURNAROUND_DB}    1.5

*** Keywords ***
Set Up M17 Receive
    Set Radio Volume Once
    Operator Does    Set the radio to M17, ${FREQUENCY_MHZ} MHz, no encryption, any destination accepted, volume unchanged

Capture Decoded Tone
    [Documentation]    One unbroken stream of the tone, ${seconds} s long, and 3 s of
    ...                the speaker from a second after it starts: no gap between
    ...                transmissions falls in the capture.
    [Arguments]    ${seconds}=6
    ${frames}=    Evaluate    int(${seconds} / 0.04)
    Start Transmitting M17 Stream    ${SOURCE}    audio=${TONE}    frames=${frames}    loop=${False}
    Wait Seconds    1
    ${a}=    Capture Radio Audio    3    1000
    Stop Transmitting
    RETURN    ${a}

*** Test Cases ***
M17 RX Shows The Callsign
    [Documentation]    M17-RX-1: the generated stream decodes on the radio.
    [Tags]    manual
    Set Up M17 Receive
    Start Transmitting M17 Stream    ${SOURCE}    audio=${TONE}
    Operator Confirms    Each transmission shows ${SOURCE} and plays a steady tone
    [Teardown]    Stop Transmitting

M17 RX Plays The Codec2 Tone
    [Documentation]    M17-RX-1: the speaker plays the 1 kHz tone the stream carries.
    [Tags]    needs:audio
    Set Up M17 Receive
    Start Transmitting M17 Stream    ${SOURCE}    audio=${TONE}    frames=100    gap_s=0.2
    Wait Seconds    1
    ${a}=    Capture Radio Audio    3
    Value Should Be Within    ${a}[tone_hz]    1000    30    decoded tone (Hz)    relative=False
    [Teardown]    Stop Transmitting

M17 RX Sensitivity
    [Documentation]    M17-RX-2: the lowest level at the antenna port at which the tone still
    ...                plays with ${MIN_SINAD_DB} dB SINAD: a baseline for future work.
    [Tags]    needs:audio    needs:abs-level
    Set Up M17 Receive
    Find M17 RX Sensitivity    min_sinad_db=${MIN_SINAD_DB}    source=${SOURCE}

M17 RX Tolerates A Carrier Offset
    [Documentation]    M17-RX-3: the stream off frequency by up to 2 kHz either way.
    [Tags]    manual
    Set Up M17 Receive
    FOR    ${offset}    IN    @{OFFSETS_HZ}
        Start Transmitting M17 Stream    ${SOURCE}    audio=${TONE}    offset_hz=${offset}
        Run Keyword And Continue On Failure
        ...    Operator Confirms    ${offset} Hz off: each transmission shows ${SOURCE} and plays the tone cleanly
        Stop Transmitting
    END
    [Teardown]    Stop Transmitting

M17 RX Plays Right After A Transmission
    [Documentation]    M17-RX-5: the radio receives normally straight after it transmits:
    ...                the decoded tone at the level it had before the key-up. The
    ...                radio transmits into the attenuator chain, so it must be on low
    ...                power, with the power-rated attenuator at the radio's end.
    [Tags]    needs:audio
    Set Up M17 Receive
    Operator Does    Set the radio's power to low: it transmits into the attenuator chain in this test
    ${before}=    Capture Decoded Tone
    Key Up    2    gap=0
    ${after}=    Capture Decoded Tone
    Value Should Be Within    ${after}[tone_hz]    1000    30    decoded tone after TX (Hz)    relative=False
    ${change}=    Evaluate    20 * math.log10(${after}[tone_amplitude] / ${before}[tone_amplitude])
    Value Should Be Within    ${change}    0    ${TURNAROUND_DB}    tone level after TX / before (dB)
    ...    relative=False
    [Teardown]    Stop Transmitting

M17 RX Is Not Interrupted By Key Presses
    [Documentation]    M17-RX-6: with voice prompts on and key beeps off, key presses during
    ...                reception must not interrupt the decoded audio. One unbroken 30 s
    ...                stream, so any dropout is the radio's.
    [Tags]    manual    needs:audio
    Set Up M17 Receive
    Operator Does    Turn voice prompts on and key beeps off. When you continue, the speaker is recorded for 8 s: press a few keys (menu, up, down, back) during it, without changing channel
    Start Transmitting M17 Stream    ${SOURCE}    audio=${TONE}    frames=750    loop=${False}
    Wait Seconds    1
    ${a}=    Capture Radio Audio    8    1000
    Audio Should Have No Dropouts    ${a}
    [Teardown]    Stop Transmitting

M17 RX Speaker Images
    [Documentation]    M17-RX-4: Codec2's 8 kHz audio raised to 48 kHz without a filter
    ...                repeats the 1 kHz tone around multiples of 8 kHz; their level
    ...                relative to the tone.
    [Tags]    needs:audio
    Set Up M17 Receive
    Start Transmitting M17 Stream    ${SOURCE}    audio=${TONE}    frames=100    gap_s=0.2
    Wait Seconds    1
    ${a}=    Capture Radio Audio    3
    Measure Audio Images    ${a}    1000    7000    9000    15000    17000
    [Teardown]    Stop Transmitting
