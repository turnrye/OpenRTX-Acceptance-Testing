# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     FM follows the 12.5/25 kHz channel bandwidth setting, on transmit
...               and on receive, on every band the radio has.
...
...               Run it with a bench's argument file (bench/example.args), whose
...               oat.expand repeats each test on every band in the radio file. Transmit is measured from the SDR capture. Receive is judged by
...               the operator, or, on a bench with an audio interface, measured
...               from its recording of the speaker once Set Radio Volume Once has
...               set the volume by level.
...               The programming cable stays out throughout: it disconnects the mic
...               and the speaker.
...
...               A bench with an audio interface on the accessory jack keys the radio
...               and plays the test tones into the mic itself; otherwise the operator
...               plays a tone at the mic, whistles and keys.
...
...               To measure stock firmware as a reference, exclude mode:m17.
Library           oat.robot_library.OpenRTXAcceptance
Resource          ../keyed.resource
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
Suite Teardown    Close Bench
Test Tags         per-band    mode:fm

*** Variables ***
# Per tester and per run, so no defaults: pass them with -v NAME:value
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
# The DSP image, mandatory for radio files with dsp_image: true
${DSP}            ${EMPTY}
# Narrow over wide deviation of the same tone at the mic, read from the tone
# itself so a key-up transient doesn't count: the BK4819's
# 12.5 kHz mode halves it (0.47-0.60 measured on a C2, 2026-10-30; the spread
# is the acoustic tone moving between takes)
${RATIO_LOW}      0.4
${RATIO_HIGH}     0.65
${CTCSS_HZ}       100.0
# How far CTCSS deviation and RX SINAD may move after a trip through M17
# (MODE-1): about the repeatability of the bench
${MODE_RATIO_LOW}     0.9
${MODE_RATIO_HIGH}    1.1
${MODE_SINAD_DB}      1.0
# Level at the antenna port for the RX SINAD comparison: a few dB above
# 12 dB SINAD, where SINAD still moves with the receiver's noise figure
${MODE_LEVEL_DBM}     -110
# Interferer level over the wanted signal. A check that a neighbour doesn't
# break through, not a selectivity figure: at 30 dB a HackRF hits its gain
# limit and leaves the wanted signal near the radio's threshold (C2, 2026-10-30)
${ADJACENT_DB}    20
# Tone takes injected through an audio interface: a steady 1 kHz tone in the
# clean range, then a loud 2 kHz one that drives the limiter as a whistle does.
# audio-levels.robot finds the range: a Digirig on a C62 is clean up to
# -30 dBFS and limits from -25 dBFS (2026-11-07)
${TONE_DBFS}      -35
${LOUD_HZ}        2000
${LOUD_DBFS}      -10
# Receive tests measured through an audio interface. A full-deviation tone
# must be louder than the nominal one by the deviation step within this, and
# with the interferer ${ADJACENT_DB} dB up its 400 Hz tone must stay this far
# under the wanted 1 kHz tone and SINAD drop no more than this. Bench
# judgement, tune later. Clean means the radio file's rx THD+N limit.
${STEP_TOLERANCE_DB}         1.5
${MAX_INTERFERER_TONE_DB}    -20
${MAX_SINAD_DROP_DB}         3

*** Keywords ***
Capture Tone Takes
    [Documentation]    A 5 s take of a steady 1 kHz tone and, with loud, one that drives
    ...                the limiter. The bench plays and keys them where it has an audio
    ...                interface with PTT; otherwise the operator follows ${instruction}.
    [Arguments]    ${instruction}    ${loud}=${True}
    ${count}=    Set Variable If    ${loud}    2    1
    ${keys}=    Bench Keys The Radio
    ${audio}=    Bench Has Audio
    IF    not (${keys} and ${audio})
        ${tx}=    Capture Radio Transmissions    ${instruction}    expected=${count}
        RETURN    ${tx}
    END
    Start Capture
    Wait Seconds    1
    Inject Audio Tone    1000    ${TONE_DBFS}
    Key Up    5
    IF    ${loud}
        Inject Audio Tone    ${LOUD_HZ}    ${LOUD_DBFS}
        Key Up    5
    END
    Stop Injecting Audio
    ${tx}=    Stop Capture And Measure FM    expected=${count}
    RETURN    ${tx}

Take Received Tone
    [Documentation]    The speaker, recorded for 3 s, receiving a 1 kHz tone at a deviation.
    [Arguments]    ${deviation}
    Start Transmitting FM Tone    ${deviation}
    Wait Seconds    1
    ${take}=    Capture Radio Audio    3    1000
    Stop Transmitting
    RETURN    ${take}

Measure Clean Reception
    [Documentation]    The speaker at the nominal and at full deviation: both clean (the
    ...                radio file's rx THD+N limit), full deviation louder by the deviation
    ...                step. Returns the full-deviation take.
    [Arguments]    ${nominal_hz}    ${full_hz}
    ${nominal}=    Take Received Tone    ${nominal_hz}
    ${full}=    Take Received Tone    ${full_hz}
    Run Keyword And Continue On Failure    Audio Distortion Should Meet Limits    ${nominal}    rx
    Run Keyword And Continue On Failure    Audio Distortion Should Meet Limits    ${full}    rx
    ${step}=    Evaluate    20 * math.log10($full['tone_amplitude'] / $nominal['tone_amplitude'])
    ${expected}=    Evaluate    20 * math.log10(${full_hz} / ${nominal_hz})
    Value Should Be Within    ${step}    ${expected}    ${STEP_TOLERANCE_DB}
    ...    full over nominal deviation level (dB)    relative=False
    RETURN    ${full}

Take With Interferer
    [Documentation]    The speaker, recorded for 3 s, receiving the wanted 1 kHz tone with a
    ...                400 Hz interferer ${ADJACENT_DB} dB up at an offset (or the wanted
    ...                signal alone), and the interferer's tone relative to the wanted one.
    [Arguments]    ${deviation}    ${offset}    ${interferer_deviation}    ${interferer}=${True}
    Start Transmitting With Interferer    ${deviation}    ${offset}    ${ADJACENT_DB}
    ...    ${interferer_deviation}    interferer=${interferer}
    Wait Seconds    1
    ${take}=    Capture Radio Audio    3    1000
    Stop Transmitting
    ${tone_db}=    Audio Component Db    ${take}    400
    RETURN    ${take}    ${tone_db}

Neighbour Should Not Break Through
    [Documentation]    The interferer's 400 Hz tone stays under the wanted tone, and SINAD
    ...                drops little from the wanted signal alone.
    [Arguments]    ${alone}    ${take}    ${tone_db}    ${where}
    Run Keyword And Continue On Failure    Value Should Be At Most    ${tone_db}
    ...    ${MAX_INTERFERER_TONE_DB}    400 Hz from the channel ${where} over the 1 kHz tone (dB)
    ${drop}=    Evaluate    $alone['sinad_db'] - $take['sinad_db']
    Value Should Be At Most    ${drop}    ${MAX_SINAD_DROP_DB}
    ...    SINAD drop with the channel ${where} (dB)

*** Test Cases ***
FM TX At 25 kHz Stays Within Wide Limits
    [Tags]    tx
    ${tone}=    Create Audio Tone    1000
    ${cabling}=    Bench Cabling
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, low power, no CTCSS, ${cabling}
    ${tx}=    Capture Tone Takes
    ...    Play ${tone} at a fixed volume and distance from the mic, key PTT for 5 s, release. Then key PTT, whistle loudly close to the mic for 5 s, release.
    Remember Result    wide tone    ${tx}[0]
    FM Transmission Should Meet Limits    ${tx}[0]    BW_25
    FM Transmission Should Meet Limits    ${tx}[1]    BW_25

FM TX At 12.5 kHz Stays Within Narrow Limits
    [Tags]    tx
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, low power, no CTCSS
    ${tx}=    Capture Tone Takes
    ...    Play the same tone at the same volume and distance, key PTT for 5 s, release. Then key PTT, whistle loudly close to the mic for 5 s, release.
    Remember Result    narrow tone    ${tx}[0]
    FM Transmission Should Meet Limits    ${tx}[0]    BW_12_5
    FM Transmission Should Meet Limits    ${tx}[1]    BW_12_5

Narrow FM TX Deviates Less Than Wide
    [Tags]    tx
    ${wide}=    Recall Result    wide tone
    ${narrow}=    Recall Result    narrow tone
    Skip If    $wide is None or $narrow is None
    ...    Needs the tone takes of both bandwidth tests on this band
    Ratio Should Be Between    ${narrow}[tone_deviation_hz]    ${wide}[tone_deviation_hz]
    ...    ${RATIO_LOW}    ${RATIO_HIGH}    narrow/wide tone deviation

FM RX At 25 kHz Is Clean Up To Full Deviation
    [Tags]    rx    manual
    ${volume}=    Set Listening Volume
    ${audio}=    Bench Has Audio
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, no CTCSS, ${volume}
    IF    ${audio}
        ${full}=    Measure Clean Reception    3000    5000
        Remember Result    wide full tone    ${full}
    ELSE
        Start Transmitting FM Tone    3000
        Operator Confirms    The radio plays a clean, steady 1 kHz tone
        Stop Transmitting
        Start Transmitting FM Tone    5000
        Operator Confirms    The radio plays the tone louder and still undistorted
    END
    [Teardown]    Stop Transmitting

FM RX At 12.5 kHz Is Clean Up To Full Deviation
    [Tags]    rx    manual
    ${volume}=    Set Listening Volume    volume unchanged
    ${audio}=    Bench Has Audio
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, no CTCSS, ${volume}
    IF    ${audio}
        ${full}=    Measure Clean Reception    1500    2500
        ${wide}=    Recall Result    wide full tone
        IF    $wide is not None
            ${louder}=    Evaluate    20 * math.log10($full['tone_amplitude'] / $wide['tone_amplitude'])
            Record Value    12.5 kHz full-deviation level over 25 kHz    ${louder}    dB
        END
    ELSE
        Start Transmitting FM Tone    1500
        Operator Confirms    The radio plays a clean, steady 1 kHz tone
        Stop Transmitting
        Start Transmitting FM Tone    2500
        Operator Confirms    The radio plays the tone louder and still undistorted
        Operator Reports    Compared with the louder tone at 25 kHz, is this quieter, the same, or louder?
    END
    [Teardown]    Stop Transmitting

FM Bandwidth Survives An M17 Round Trip
    [Tags]    tx    mode:m17
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, low power, then switch to M17 and back to FM
    ${tx}=    Capture Tone Takes
    ...    Play the same tone at the same volume and distance, key PTT for 5 s, release.
    ...    loud=${False}
    FM Transmission Should Meet Limits    ${tx}[0]    BW_12_5
    ${narrow}=    Recall Result    narrow tone
    Skip If    $narrow is None    Needs the 12.5 kHz tone take on this band to compare with
    Ratio Should Be Between    ${tx}[0][tone_deviation_hz]    ${narrow}[tone_deviation_hz]
    ...    0.8    1.25    tone deviation after M17 / before

CTCSS Deviation Follows The Bandwidth
    [Tags]    tx    ctcss
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, low power, ${CTCSS_HZ} Hz CTCSS tone on transmit
    ${wide}=    Capture Keyed FM Transmissions    5
    Remember Result    wide ctcss    ${wide}[0]
    Operator Does    Set the bandwidth to 12.5 kHz, CTCSS tone unchanged
    ${narrow}=    Capture Keyed FM Transmissions    5
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

FM Restores CTCSS After M17
    [Documentation]    MODE-1: switching to M17 and back must restore the FM setup,
    ...                CTCSS level included (radio_setOpmode). Compares with the
    ...                level before M17; CTCSS Level Meets Limits checks the limits.
    [Tags]    tx    ctcss    mode:m17
    ${before}=    Recall Result    narrow ctcss
    Skip If    $before is None    Needs the 12.5 kHz CTCSS take of the CTCSS test on this band
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, low power, ${CTCSS_HZ} Hz CTCSS tone on transmit, then switch to M17
    ${keys}=    Bench Keys The Radio
    IF    ${keys}
        Key Up    2
        Operator Does    Switch back to FM
    ELSE
        Operator Does    Key PTT once for 2 s, then switch back to FM
    END
    ${after}=    Capture Keyed FM Transmissions    5
    Ratio Should Be Between    ${after}[0][ctcss_deviation_hz]    ${before}[ctcss_deviation_hz]
    ...    ${MODE_RATIO_LOW}    ${MODE_RATIO_HIGH}    CTCSS deviation after M17 / before
    [Teardown]    Operator Does    Turn the CTCSS tone off

FM TX Power Rises With Each Setting
    [Documentation]    Relative levels through the bench: no power meter needed. For
    ...                stock firmware, name its settings: -v POWER_SETTINGS:Low,High
    [Tags]    tx    power
    ${settings}=    Power Settings
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, no CTCSS, power ${settings}[0]
    ${tx}=    Capture Keyed Power Steps    ${settings}
    TX Power Should Rise With Setting    ${settings}    ${tx}
    [Teardown]    Run Keyword If    $TEST_STATUS != 'SKIP'
    ...    Operator Does    Set the power back to ${settings}[0]

FM RX At 12.5 kHz Rejects An Adjacent Channel
    [Tags]    rx    manual    adjacent
    ${volume}=    Set Listening Volume
    ${audio}=    Bench Has Audio
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, no CTCSS, ${volume}
    IF    ${audio}
        ${alone}    ${_}=    Take With Interferer    1500    12500    2500    interferer=${False}
        ${above}    ${above_db}=    Take With Interferer    1500    12500    2500
        ${below}    ${below_db}=    Take With Interferer    1500    -12500    2500
        Run Keyword And Continue On Failure
        ...    Neighbour Should Not Break Through    ${alone}    ${above}    ${above_db}    above
        Neighbour Should Not Break Through    ${alone}    ${below}    ${below_db}    below
    ELSE
        Start Transmitting With Interferer    1500    12500    ${ADJACENT_DB}    2500    interferer=off
        Operator Confirms    The wanted signal alone: a clean, steady 1 kHz tone
        Stop Transmitting
        Start Transmitting With Interferer    1500    12500    ${ADJACENT_DB}    2500
        Operator Confirms    The 1 kHz tone continues with no 400 Hz tone from the channel above
        Operator Reports    Any added noise compared with the wanted signal alone?
        Stop Transmitting
        Start Transmitting With Interferer    1500    -12500    ${ADJACENT_DB}    2500
        Operator Confirms    With the interferer on the channel below: still no 400 Hz tone
    END
    [Teardown]    Stop Transmitting

FM RX At 25 kHz Rejects An Adjacent Channel
    [Tags]    rx    manual    adjacent
    ${volume}=    Set Listening Volume
    ${audio}=    Bench Has Audio
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 25 kHz bandwidth, no CTCSS, ${volume}
    IF    ${audio}
        ${alone}    ${_}=    Take With Interferer    3000    25000    5000    interferer=${False}
        ${above}    ${above_db}=    Take With Interferer    3000    25000    5000
        Run Keyword And Continue On Failure
        ...    Neighbour Should Not Break Through    ${alone}    ${above}    ${above_db}    above
        # Not a requirement: what the wide filter lets through from a narrow neighbour
        ${narrow}    ${narrow_db}=    Take With Interferer    3000    12500    2500
        Record Value    400 Hz from a 12.5 kHz neighbour over the 1 kHz tone    ${narrow_db}    dB
        ${drop}=    Evaluate    $alone['sinad_db'] - $narrow['sinad_db']
        Record Value    SINAD drop with a 12.5 kHz neighbour    ${drop}    dB
    ELSE
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
    END
    [Teardown]    Stop Transmitting

FM RX SINAD Is Unchanged After M17
    [Documentation]    MODE-1: receive SINAD and tone level at a fixed level before and
    ...                after a trip through M17: ${MODE_LEVEL_DBM} dBm with a calibrated
    ...                transmitter, otherwise the bench's transmitter gain.
    [Tags]    rx    mode:m17    needs:audio
    Set Radio Volume Once
    Operator Does    Set the radio to FM, ${FREQUENCY_MHZ} MHz, 12.5 kHz bandwidth, no CTCSS, squelch open, volume unchanged
    ${dev}=    Test Deviation    BW_12_5
    ${calibrated}=    Bench Transmits At Calibrated Levels
    ${level}=    Set Variable If    ${calibrated}    ${MODE_LEVEL_DBM}    ${None}
    Start Transmitting FM Tone    ${dev}    level_dbm=${level}
    ${before}=    Capture Radio Audio    3    1000    psophometric
    Stop Transmitting
    Operator Does    Switch the radio to M17, then back to FM, everything else unchanged
    Start Transmitting FM Tone    ${dev}    level_dbm=${level}
    ${after}=    Capture Radio Audio    3    1000    psophometric
    Value Should Be Within    ${after}[sinad_db]    ${before}[sinad_db]    ${MODE_SINAD_DB}
    ...    SINAD after M17 (dB)    relative=False
    ${change}=    Evaluate    20 * math.log10(${after}[tone_amplitude] / ${before}[tone_amplitude])
    Value Should Be Within    ${change}    0    ${MODE_SINAD_DB}    tone level after M17 / before (dB)
    ...    relative=False
    [Teardown]    Stop Transmitting
