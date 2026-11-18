/*
 * SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * M17 receive trace, see m17_trace.hpp and README.md. Lines are ASCII,
 * fields separated by single spaces, times in ms since boot:
 *
 *   ID <label>                       answer to "ID"
 *   ON <ms> / OFF <ms>               answer to "TRACE ON" / "TRACE OFF"
 *   HB <ms> <stack> <free> <dropped> once a second: receive thread stack
 *                                    size, its least free stack since
 *                                    boot, lines dropped so far
 *   LOCK <ms> <0|1>                  demodulator lock changed
 *   LSF <ms> <valid> <F|I> <hex>     link setup data changed: its 30 bytes,
 *                                    from a link setup frame or the LICH
 *   STR <ms> <fn> <lsf> <hex>        stream frame: the frame number and 16
 *                                    payload bytes the decoder holds (the
 *                                    previous ones when it rejected this
 *                                    frame), and whether the LSF is valid
 *   PKT <ms> <counter> <eof> <hex>   packet frame: counter, end flag and
 *                                    the 25 data bytes
 *   EOT <ms>                         end of transmission frame
 *   FRM <ms> <type>                  any other frame type
 *   PLAY <ms> <fn>                   a stream frame went to the speaker
 */

#include "rtx/m17_trace.hpp"
#include "interfaces/delays.h"
#include "core/memory_profiling.h"
#include "core/threads.h"
#include <pthread.h>
#include <atomic>
#include <cstring>

extern "C" ssize_t vcom_readBlock(void *buf, size_t len);
extern "C" ssize_t vcom_writeBlock(const void *buf, size_t len);

using namespace M17;

/// Ring buffer between the receive thread and the writer thread.
static constexpr size_t RING_SIZE = 4096;
/// Largest write: one USB full speed bulk packet.
static constexpr size_t WRITE_CHUNK = 64;
/// Longest line, LSF with its 60 hex digits.
static constexpr size_t LINE_SIZE = 112;
/// Longest command from the host.
static constexpr size_t COMMAND_SIZE = 16;
/// Period of the stack report.
static constexpr long long HEARTBEAT_MS = 1000;
/// Pause of the writer thread when the ring is empty.
static constexpr unsigned int WRITER_IDLE_MS = 5;
/// Stack of the writer thread.
static constexpr size_t WRITER_STACK = 1024;

static char ring[RING_SIZE];               ///< Lines waiting to be written.
static std::atomic<uint32_t> head{0};      ///< Next byte the producer fills.
static std::atomic<uint32_t> tail{0};      ///< Next byte the writer sends.
static std::atomic<bool> tracing{false};   ///< The host asked for the trace.
static bool writerStarted = false;         ///< The writer thread is running.
static uint32_t dropped = 0;               ///< Lines that didn't fit.
static long long lastHeartbeat = 0;        ///< Time of the last HB line.
static bool lastLock = false;              ///< Lock state last reported.

static char line[LINE_SIZE];               ///< Line being formatted.
static size_t lineLen = 0;                 ///< Characters in line.
static char command[COMMAND_SIZE];         ///< Command being received.
static size_t commandLen = 0;              ///< Characters in command.

static LinkSetupFrame lastLsf;             ///< Link setup data last reported.
static LinkSetupFrame lsfCopy;             ///< Scratch copy of the decoder's.
static StreamFrame streamCopy;             ///< Scratch copy of the decoder's.

/**
 * Write out the ring buffer, in chunks, on its own thread: a USB write
 * waits for the host, which must never hold up reception.
 *
 * @param arg: unused.
 * @return never.
 */
static void *writer(void *arg)
{
    (void) arg;

    while (true) {
        uint32_t t = tail.load(std::memory_order_relaxed);
        uint32_t h = head.load(std::memory_order_acquire);
        if (t == h) {
            delayMs(WRITER_IDLE_MS);
            continue;
        }

        size_t start = t % RING_SIZE;
        size_t len = h - t;
        if (len > WRITE_CHUNK)
            len = WRITE_CHUNK;
        if (len > RING_SIZE - start)
            len = RING_SIZE - start;

        vcom_writeBlock(&ring[start], len);
        tail.store(t + len, std::memory_order_release);
    }

    return nullptr;
}

/**
 * Start the writer thread, once.
 */
static void startWriter()
{
    if (writerStarted)
        return;

    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, WRITER_STACK);
#ifdef _MIOSIX
    // Below the receive and UI threads: it only drains the ring buffer
    struct sched_param param;
    param.sched_priority = THREAD_PRIO_LOW;
    pthread_attr_setschedparam(&attr, &param);
#endif
    pthread_t thread;
    writerStarted = (pthread_create(&thread, &attr, writer, nullptr) == 0);
}

/**
 * Start a line with its keyword.
 *
 * @param keyword: the line's keyword.
 */
static void begin(const char *keyword)
{
    lineLen = 0;
    while ((*keyword != '\0') && (lineLen < LINE_SIZE))
        line[lineLen++] = *keyword++;
}

/**
 * Append a space and a decimal number.
 *
 * @param value: the number.
 */
static void dec(unsigned long long value)
{
    char digits[20];
    size_t n = 0;

    do {
        digits[n++] = static_cast<char>('0' + (value % 10));
        value /= 10;
    } while ((value != 0) && (n < sizeof(digits)));

    if (lineLen < LINE_SIZE)
        line[lineLen++] = ' ';
    while ((n > 0) && (lineLen < LINE_SIZE))
        line[lineLen++] = digits[--n];
}

/**
 * Append a space and bytes as hex digits.
 *
 * @param data: the bytes.
 * @param len: how many.
 */
static void hex(const uint8_t *data, size_t len)
{
    static constexpr char DIGITS[] = "0123456789abcdef";

    if (lineLen < LINE_SIZE)
        line[lineLen++] = ' ';
    for (size_t i = 0; (i < len) && ((lineLen + 2) <= LINE_SIZE); i++) {
        line[lineLen++] = DIGITS[data[i] >> 4];
        line[lineLen++] = DIGITS[data[i] & 0x0F];
    }
}

/**
 * Append a space and one character.
 *
 * @param c: the character.
 */
static void chr(char c)
{
    if ((lineLen + 2) <= LINE_SIZE) {
        line[lineLen++] = ' ';
        line[lineLen++] = c;
    }
}

/**
 * End the line and queue it for the writer, or count it as dropped when
 * the ring is full. The receive thread is the only producer.
 */
static void send()
{
    if (lineLen < LINE_SIZE)
        line[lineLen++] = '\n';

    uint32_t h = head.load(std::memory_order_relaxed);
    uint32_t t = tail.load(std::memory_order_acquire);
    if ((RING_SIZE - (h - t)) < lineLen) {
        dropped++;
        return;
    }

    for (size_t i = 0; i < lineLen; i++)
        ring[(h + i) % RING_SIZE] = line[i];
    head.store(h + static_cast<uint32_t>(lineLen), std::memory_order_release);
}

/**
 * Start a line with its keyword and the time.
 *
 * @param keyword: the line's keyword.
 */
static void event(const char *keyword)
{
    begin(keyword);
    dec(static_cast<unsigned long long>(getTick()));
}

/**
 * Act on a complete command from the host.
 */
static void handleCommand()
{
    startWriter();

    if (strcmp(command, "ID") == 0) {
        begin("ID");
        if (lineLen < LINE_SIZE)
            line[lineLen++] = ' ';
        for (const char *l = M17_TRACE_LABEL;
             (*l != '\0') && (lineLen < LINE_SIZE); l++)
            line[lineLen++] = *l;
        send();
    } else if (strcmp(command, "TRACE ON") == 0) {
        tracing.store(true);
        lastLsf.clear();
        lastHeartbeat = 0;
        event("ON");
        send();
    } else if (strcmp(command, "TRACE OFF") == 0) {
        event("OFF");
        send();
        tracing.store(false);
    }
}

void m17trace_poll()
{
    char c;
    while (vcom_readBlock(&c, 1) == 1) {
        if ((c == '\r') || (c == '\n')) {
            command[commandLen] = '\0';
            if (commandLen > 0)
                handleCommand();
            commandLen = 0;
        } else if (commandLen < (COMMAND_SIZE - 1)) {
            command[commandLen++] = c;
        }
    }

    if (!tracing.load())
        return;

    long long now = getTick();
    if ((now - lastHeartbeat) >= HEARTBEAT_MS) {
        lastHeartbeat = now;
        event("HB");
        dec(getStackSize());
        dec(getAbsoluteFreeStack());
        dec(dropped);
        send();
    }
}

void m17trace_lock(bool lock)
{
    if (lock == lastLock)
        return;

    lastLock = lock;
    if (!tracing.load())
        return;

    event("LOCK");
    dec(lock ? 1 : 0);
    send();
}

void m17trace_frame(uint8_t type, FrameDecoder &decoder)
{
    if (!tracing.load())
        return;

    FrameType frameType = static_cast<FrameType>(type);

    lsfCopy = decoder.getLsf();
    if (memcmp(lsfCopy.getData(), lastLsf.getData(),
               sizeof(LinkSetupFrame)) != 0) {
        lastLsf = lsfCopy;
        event("LSF");
        dec(lsfCopy.valid() ? 1 : 0);
        chr((frameType == FrameType::LINK_SETUP) ? 'F' : 'I');
        hex(lsfCopy.getData(), sizeof(LinkSetupFrame));
        send();
    }

    switch (frameType) {
        case FrameType::STREAM:
            streamCopy = decoder.getStreamFrame();
            event("STR");
            dec(streamCopy.getFrameNumber());
            dec(lsfCopy.valid() ? 1 : 0);
            hex(streamCopy.data(), sizeof(payload_t));
            send();
            break;

        case FrameType::PACKET: {
            const PacketFrame &packet = decoder.getPacketFrame();
            event("PKT");
            dec(packet.getCounter());
            dec(packet.isEof() ? 1 : 0);
            hex(packet.data(), PacketFrame::DATA_SIZE);
            send();
            break;
        }

        case FrameType::EOT:
            event("EOT");
            send();
            break;

        case FrameType::LINK_SETUP:
            break;

        default:
            event("FRM");
            dec(type);
            send();
            break;
    }
}

void m17trace_played(StreamFrame &frame)
{
    if (!tracing.load())
        return;

    event("PLAY");
    dec(frame.getFrameNumber());
    send();
}
