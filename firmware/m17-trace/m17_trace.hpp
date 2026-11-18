/*
 * SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * M17 receive trace: reports what the M17 receiver decoded, one text line
 * per event, over the radio's USB serial port, for oat's trace suite. Test
 * builds only: apply_trace.py adds it to an OpenRTX tree, see README.md.
 *
 * Silent until the host sends "TRACE ON". The receive thread only formats
 * lines into a ring buffer; a low priority thread writes them out, so a
 * slow or absent host never blocks reception (lines that don't fit are
 * counted as dropped).
 */

#ifndef M17_TRACE_H
#define M17_TRACE_H

#ifndef __cplusplus
#error This header is C++ only!
#endif

#include <cstdint>
#include "protocols/M17/FrameDecoder.hpp"
#include "protocols/M17/StreamFrame.hpp"

#ifndef M17_TRACE_LABEL
#define M17_TRACE_LABEL "unlabelled"
#endif

/**
 * Handle the host's commands, and once a second while tracing report the
 * receive thread's stack. Call from the receive thread, every update.
 */
void m17trace_poll();

/**
 * Report a change of the demodulator lock.
 *
 * @param lock: true while the demodulator is locked.
 */
void m17trace_lock(bool lock);

/**
 * Report a frame the decoder has just decoded, and the link setup data when
 * it has changed.
 *
 * @param type: M17::FrameType of the frame.
 * @param decoder: the decoder, after decoding the frame.
 */
void m17trace_frame(uint8_t type, M17::FrameDecoder &decoder);

/**
 * Report that a stream frame went to the speaker.
 *
 * @param frame: the stream frame.
 */
void m17trace_played(M17::StreamFrame &frame);

#endif // M17_TRACE_H
