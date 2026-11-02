# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     M17 on transmit, measured and decoded from the SDR capture
...               (oat.m17): the 4FSK deviations and their ratio, symbol rate and
...               carrier, bandwidth and EVM, decoding, preamble, long transmissions,
...               power, and the Codec2 input through the audio interface.
...
...               Limits are the radio file's m17 block: nominal values from the M17
...               specification, tolerances as acceptance criteria. Decoding uses
...               this library's own decoder; with m17-demod installed each capture
...               is also run through it, and with Codec2 the voice is decoded to a
...               WAV. Set the radio's M17 callsign to ${CALLSIGN} first, or pass
...               -v CALLSIGN:<yours>.
Library           Collections
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
Suite Teardown    Close Bench
Test Tags         per-band    mode:m17    tx

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
${DSP}            ${EMPTY}
# The callsign set on the radio, checked in every decoded LSF
${CALLSIGN}       N0CALL
# Key-ups for the decode rate (M17-TX-5)
${DECODE_RUNS}    10
# The long transmission (M17-TX-7): long enough to show heap trouble
${LONG_S}         20
# Speech levels into the mic for the Codec2 input level (M17-TX-8), dBFS
# relative to the file as recorded
@{SPEECH_GAINS_DB}    -20    -10    0
${SPEECH_WAV}     ${EMPTY}
# A reference tone and the two above Codec2's 4 kHz band (M17-TX-9); at an
# 8 kHz sampling rate 5 and 7 kHz fold back to 3 and 1 kHz
${ALIAS_LEVEL_DBFS}    -30

*** Keywords ***
Set Up M17
    Operator Does    Set the radio to M17, ${FREQUENCY_MHZ} MHz, callsign ${CALLSIGN}, destination @ALL or empty, low power, no encryption

Capture Keyed M17
    [Documentation]    One M17 transmission of ${seconds} s keyed through Key Radio,
    ...                with a second of silence on each side.
    [Arguments]    ${seconds}=5
    Start Capture
    Wait Seconds    1
    Key Radio
    Wait Seconds    ${seconds}
    Release Radio
    Wait Seconds    1
    ${tx}=    Stop Capture And Measure M17
    RETURN    ${tx}[0]

*** Test Cases ***
M17 TX Deviation And Ratio Meet The Specification
    [Documentation]    M17-TX-1 and 2: outer and inner deviation (nominally 2400 and
    ...                800 Hz) and their ratio (3).
    Set Up M17
    ${tx}=    Capture Radio M17 Transmissions    Key PTT for 5 s without speaking, release.
    Remember Result    m17 take    ${tx}[0]
    M17 Transmission Should Meet Limits    ${tx}[0]    deviation,ratio

M17 TX Symbol Rate And Carrier Are On Frequency
    [Documentation]    M17-TX-3, on the take of the previous test.
    ${tx}=    Recall Result    m17 take
    Skip If    $tx is None    Needs the M17 take of the deviation test on this band
    M17 Transmission Should Meet Limits    ${tx}    symbol_rate,frequency

M17 TX Bandwidth And EVM
    [Documentation]    M17-TX-4: 99% occupied bandwidth, and EVM after the receive RRC
    ...                filter, on the same take.
    ${tx}=    Recall Result    m17 take
    Skip If    $tx is None    Needs the M17 take of the deviation test on this band
    M17 Transmission Should Meet Limits    ${tx}    bandwidth,evm

M17 TX Decodes Every Time
    [Documentation]    M17-TX-5: ${DECODE_RUNS} key-ups, each with an LSF that passes its
    ...                CRC and names ${CALLSIGN}, stream frames in sequence, an EOT, and
    ...                the right polarity.
    Set Up M17
    ${tx}=    Capture Radio M17 Transmissions
    ...    Key PTT ${DECODE_RUNS} times, 2 s each without speaking, with 2 s between.
    ...    expected=${DECODE_RUNS}
    M17 Transmissions Should Decode    ${tx}    ${CALLSIGN}
    FOR    ${m}    IN    @{tx}
        M17 Transmission Should Meet Limits    ${m}    polarity
    END

M17 TX Preamble And First LSF
    [Documentation]    M17-TX-6: the preamble (40 ms or more; OpenRTX sends 80 ms, 160 ms
    ...                after master's dccf32f85) and the time from key-up to the LSF.
    ${tx}=    Recall Result    m17 take
    Skip If    $tx is None    Needs the M17 take of the deviation test on this band
    M17 Transmission Should Meet Limits    ${tx}    preamble
    Set Test Message    preamble ${tx}[preamble_s] s, LSF ${tx}[lsf_latency_s] s after key-up

M17 TX Holds A Long Transmission
    [Documentation]    M17-TX-7: ${LONG_S} s keyed without a reset: one transmission whose
    ...                frames run in sequence to the end.
    Set Up M17
    ${tx}=    Capture Radio M17 Transmissions    Key PTT for ${{${LONG_S} + 2}} s without speaking, release.
    M17 Transmissions Should Decode    ${tx}    ${CALLSIGN}
    Should Be True    ${tx}[0][stream_frames] >= ${LONG_S} / 0.04
    ...    Only ${tx}[0][stream_frames] frames in ${LONG_S} s

M17 TX Power Rises With Each Setting
    [Documentation]    M17-TX-7: relative levels through the bench, as for FM.
    [Tags]    power
    ${settings}=    Power Settings
    ${order}=    Evaluate    ", then ".join($settings)
    Set Up M17
    Operator Does    Set the power to ${settings}[0]
    ${tx}=    Capture Radio M17 Transmissions
    ...    For each power setting in turn (${order}): set it, key PTT for 3 s without speaking, release.
    ...    expected=${{len($settings)}}
    TX Power Should Rise With Setting    ${settings}    ${tx}
    [Teardown]    Run Keyword If    $TEST_STATUS != 'SKIP'
    ...    Operator Does    Set the power back to ${settings}[0]

M17 TX Codec2 Input Level
    [Documentation]    M17-TX-8: the same speech at three levels into the mic; the decoded
    ...                voice's peak and the fraction of it near full scale show where the
    ...                mic gain clips. Needs Codec2 (c2dec) to decode.
    [Tags]    needs:audio
    Set Up M17
    ${speech}=    Set Variable If    $SPEECH_WAV    ${SPEECH_WAV}
    IF    not $speech
        ${speech}=    Create Speech-Like Audio    6
    END
    ${peaks}=    Create List
    FOR    ${gain}    IN    @{SPEECH_GAINS_DB}
        Inject Audio File    ${speech}    gain_db=${gain}    loop=True
        ${tx}=    Capture Keyed M17    6
        Stop Injecting Audio
        Skip If    'voice_file' not in $tx    No decoded voice: install codec2 (c2dec)
        Append To List    ${peaks}    ${gain} dB: peak ${tx}[voice_peak_dbfs] dBFS, ${tx}[voice_clipped] clipped
    END
    Set Test Message    ${{"; ".join($peaks)}}
    [Teardown]    Stop Injecting Audio

M17 TX Codec2 Input Aliasing
    [Documentation]    M17-TX-9: 5 and 7 kHz tones into the mic fold back to 3 and 1 kHz
    ...                if the 48 to 8 kHz decimation has no filter; their level in the
    ...                decoded voice is relative to a 1 kHz tone at the same level.
    [Tags]    needs:audio
    Set Up M17
    Inject Audio Tone    1000    ${ALIAS_LEVEL_DBFS}
    ${ref}=    Capture Keyed M17
    Inject Audio Tone    5000    ${ALIAS_LEVEL_DBFS}
    ${five}=    Capture Keyed M17
    Inject Audio Tone    7000    ${ALIAS_LEVEL_DBFS}
    ${seven}=    Capture Keyed M17
    Codec2 Alias Level    ${ref}    ${five}    3000
    Codec2 Alias Level    ${ref}    ${seven}    1000
    [Teardown]    Stop Injecting Audio
