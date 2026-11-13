# SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
#
# SPDX-License-Identifier: GPL-3.0-or-later

*** Settings ***
Documentation     APRS receive against a recording of real traffic: the transmitter
...               plays the track as FM, the radio reports every frame it decodes on
...               its KISS TNC, and the count is compared with what Dire Wolf's atest
...               decodes from the same file. See docs/aprs-recorded-benchmark.md for
...               the track, the reference file and the wiring.
...
...               The recording carries real callsigns and its author asks that it
...               never be played over the air: run this only with the transmitter
...               cabled to the radio through the attenuators, no antenna anywhere.
...               Results are recorded for comparison; there is no limit yet.
Library           String
Library           oat.robot_library.OpenRTXAcceptance
Suite Setup       Open Bench    ${BENCH}    ${CALIBRATION}    ${RADIO}    ${FIRMWARE}    ${HARDWARE}    dsp=${DSP}
Suite Teardown    Close Bench
Test Tags         per-band    mode:aprs    rx    needs:tnc    long    recording

*** Variables ***
${BENCH}          ${EMPTY}
${CALIBRATION}    ${EMPTY}
${RADIO}          ${EMPTY}
${FIRMWARE}       ${EMPTY}
${HARDWARE}       ${EMPTY}
${DSP}            ${EMPTY}
# The flat (discriminator) track as a 16-bit WAV, and atest's output for it
${TRACK}          ${EMPTY}
${REFERENCE}      ${EMPTY}
# Peak deviations to play the track at, separated by commas
${DEVIATIONS_HZ}    3000
# Part of the track, for a short first run; the whole track when empty
${START_S}        0
${DURATION_S}     ${EMPTY}

*** Test Cases ***
APRS Recording Frames Heard
    [Documentation]    APRS-REC-1: the fraction of the reference decoder's frames
    ...                the radio reports, at each deviation.
    Operator Does    Check the transmitter is cabled to the radio through the attenuators with no antenna connected, and set the radio to APRS on ${FREQUENCY_MHZ} MHz with its TNC port connected
    @{deviations}=    Split String    ${DEVIATIONS_HZ}    ,
    FOR    ${dev}    IN    @{deviations}
        Play APRS Recording    ${TRACK}    ${REFERENCE}    deviation_hz=${dev}
        ...    start_s=${START_S}    duration_s=${DURATION_S}
    END
