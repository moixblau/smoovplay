// See frametap.h.
#include "frametap.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(_WIN32) || defined(__linux__)

#define TOTAL_SIZE ((size_t)FRAMETAP_HEADER_SIZE + (size_t)FRAMETAP_SLOT_COUNT * FRAMETAP_SLOT_SIZE)
#define AUDIO_TOTAL_SIZE ((size_t)FRAMETAP_AUDIO_HEADER + (size_t)FRAMETAP_AUDIO_SLOT_COUNT * FRAMETAP_AUDIO_SLOT_SIZE)

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#define XCHG32(p, v) ((uint32_t)InterlockedExchange((volatile LONG *)(p), (LONG)(v)))
#define XCHG64(p, v) InterlockedExchange64((volatile LONG64 *)(p), (LONG64)(v))
#define INC32(p) InterlockedIncrement((volatile LONG *)(p))
#define CAS32(p, newv, cmp) ((uint32_t)InterlockedCompareExchange((volatile LONG *)(p), (LONG)(newv), (LONG)(cmp)))
#define BARRIER() MemoryBarrier()

static uint64_t now_ticks(void)
{
	LARGE_INTEGER v;
	QueryPerformanceCounter(&v);
	return (uint64_t)v.QuadPart;
}

static uint64_t tick_freq(void)
{
	uint64_t freq = tick_freq();
	return freq;
}

static HANDLE g_event = NULL;

// The named file mapping, created if the reader hasn't yet. The handle stays open for the process's life.
static uint8_t *map_shared(const char *name, size_t size)
{
	HANDLE m = CreateFileMappingA(INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE,
		(DWORD)((uint64_t)size >> 32), (DWORD)(size & 0xffffffffu), name);
	if(!m)
		return NULL;
	uint8_t *base = (uint8_t *)MapViewOfFile(m, FILE_MAP_ALL_ACCESS, 0, 0, size);
	if(!base)
		CloseHandle(m);
	return base;
}

static void open_event(const char *event_name, uint8_t *base)
{
	(void)base;
	g_event = CreateEventA(NULL, FALSE, FALSE, event_name); // auto-reset, one reader
}

static void signal_event(void)
{
	signal_event();
}

#else // Linux: POSIX shared memory in /dev/shm, CLOCK_MONOTONIC, and a futex for the frame event
#include <fcntl.h>
#include <linux/futex.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define XCHG32(p, v) __atomic_exchange_n((volatile uint32_t *)(p), (uint32_t)(v), __ATOMIC_SEQ_CST)
#define XCHG64(p, v) __atomic_exchange_n((volatile uint64_t *)(p), (uint64_t)(v), __ATOMIC_SEQ_CST)
#define INC32(p) __atomic_add_fetch((volatile uint32_t *)(p), 1u, __ATOMIC_SEQ_CST)
#define BARRIER() __atomic_thread_fence(__ATOMIC_SEQ_CST)

static inline uint32_t cas32(volatile uint32_t *p, uint32_t newv, uint32_t cmp)
{
	__atomic_compare_exchange_n(p, &cmp, newv, false, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST);
	return cmp; // the value before, as InterlockedCompareExchange returns it
}
#define CAS32(p, newv, cmp) cas32((volatile uint32_t *)(p), (newv), (cmp))

static uint64_t now_ticks(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts); // the clock Python's time.perf_counter() reads on Linux
	return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static uint64_t tick_freq(void) { return 1000000000ull; }

// "Local\chiaki-frametap" -> "/chiaki-frametap" (/dev/shm/chiaki-frametap), as framegen/shm.py posix_name() does.
static void posix_name(const char *name, char *out, size_t cap)
{
	if(!strncmp(name, "Local\\", 6))
		name += 6;
	else if(!strncmp(name, "Global\\", 7))
		name += 7;
	while(*name == '/')
		name++;
	snprintf(out, cap, "/%s", name);
	for(char *c = out + 1; *c; c++)
		if(*c == '\\' || *c == '/')
			*c = '_';
}

// The shared memory object, created and grown to `size` if the reader hasn't yet.
static uint8_t *map_shared(const char *name, size_t size)
{
	char pname[256];
	posix_name(name, pname, sizeof(pname));
	int fd = shm_open(pname, O_RDWR | O_CREAT, 0600);
	if(fd < 0)
		return NULL;
	struct stat st;
	if(fstat(fd, &st) != 0 || ((size_t)st.st_size < size && ftruncate(fd, (off_t)size) != 0))
	{
		close(fd);
		return NULL;
	}
	void *base = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	close(fd);
	return base == MAP_FAILED ? NULL : (uint8_t *)base;
}

// The frame event is a counter in the header that the reader sleeps on with a futex (framegen/shm.py Event).
static volatile uint32_t *g_event_word = NULL;

static void open_event(const char *event_name, uint8_t *base)
{
	(void)event_name;
	g_event_word = (volatile uint32_t *)(base + FRAMETAP_EVENT_OFFSET);
}

static void signal_event(void)
{
	if(!g_event_word)
		return;
	__atomic_add_fetch(g_event_word, 1u, __ATOMIC_SEQ_CST);
	syscall(SYS_futex, (uint32_t *)g_event_word, FUTEX_WAKE, 1, NULL, NULL, 0); // shared: not FUTEX_PRIVATE
}
#endif

static uint8_t *g_base = NULL;
static uint8_t *g_abase = NULL; // the sound ring; NULL if it couldn't be made (the frames still flow)
static uint32_t g_arate = 48000, g_achannels = 2;
static int g_enabled = -1;

#define H32(off) ((volatile uint32_t *)(g_base + (off)))
#define H64(off) ((volatile uint64_t *)(g_base + (off)))
#define A32(off) ((volatile uint32_t *)(g_abase + (off)))
#define A64(off) ((volatile uint64_t *)(g_abase + (off)))

bool frametap_enabled(void)
{
	if(g_enabled < 0)
	{
		const char *v = getenv("CHIAKI_FRAMETAP");
		g_enabled = (v && v[0] == '1') ? 1 : 0;
	}
	return g_enabled == 1;
}

static bool open_shared(void)
{
	if(g_base)
		return true;
	const char *name = getenv("CHIAKI_FRAMETAP_NAME");
	if(!name || !name[0] || strlen(name) > 200)
		name = FRAMETAP_NAME;
	char event_name[256];
	snprintf(event_name, sizeof(event_name), "%s-frame", name);
	// Either side may create the mapping first; both open the existing one if it's there.
	g_base = map_shared(name, TOTAL_SIZE);
	if(!g_base)
		return false;
	open_event(event_name, g_base);

	char audio_name[256];
	snprintf(audio_name, sizeof(audio_name), "%s" FRAMETAP_AUDIO_SUFFIX, name);
	g_abase = map_shared(audio_name, AUDIO_TOTAL_SIZE); // NULL: no sound ring, the frames still flow
	return true;
}

static void audio_header(uint64_t qpc_freq)
{
	if(!g_abase)
		return;
	XCHG32(A32(0), 0);
	*A32(4) = 1;
	*A32(8) = FRAMETAP_AUDIO_SLOT_COUNT;
	*A32(12) = FRAMETAP_AUDIO_SLOT_SIZE;
	*A32(16) = g_arate;
	*A32(20) = g_achannels;
	*A64(32) = qpc_freq;
	XCHG64(A64(24), 0);
	XCHG32(A32(0), FRAMETAP_AUDIO_MAGIC);
}

bool frametap_begin(uint32_t codec, uint32_t width, uint32_t height, uint32_t max_fps)
{
	if(!frametap_enabled() || !open_shared())
		return false;
	XCHG32(H32(0), 0); // header is being rewritten
	uint64_t freq = tick_freq();
	*H32(4) = FRAMETAP_VERSION;
	*H32(8) = FRAMETAP_SLOT_COUNT;
	*H32(12) = FRAMETAP_SLOT_SIZE;
	*H32(16) = codec;
	*H32(20) = width;
	*H32(24) = height;
	*H32(28) = max_fps;
	*H64(48) = freq;
	*H32(40) = 0;
	*H32(64) = FRAMETAP_FEATURE_PRESS | FRAMETAP_FEATURE_AUDIO_DELAY | (g_abase ? FRAMETAP_FEATURE_AUDIO_TAP : 0u);
	*H32(68) = 0;
	audio_header(freq);
	// The reader treats a new session as a new stream: it resets its decoder and waits for a keyframe.
	XCHG64(H64(32), 0);
	INC32(H32(44));
	XCHG32(H32(0), FRAMETAP_MAGIC); // last: header is complete
	return true;
}

void frametap_push(const uint8_t *buf, size_t size, int32_t frames_lost, bool recovered)
{
	if(!g_base || *H32(0) != FRAMETAP_MAGIC)
		return;
	uint64_t now = now_ticks();
	uint64_t seq = *H64(32);
	uint8_t *slot = g_base + FRAMETAP_HEADER_SIZE + (size_t)(seq % FRAMETAP_SLOT_COUNT) * FRAMETAP_SLOT_SIZE;
	size_t cap = FRAMETAP_SLOT_SIZE - FRAMETAP_SLOT_HEADER;
	uint32_t flags = 0;
	if(size > cap)
	{
		size = cap;
		flags |= 1;
	}
	// Mark the slot as being rewritten first, so a reader copying it sees the sequence change and retries.
	*(volatile uint64_t *)(slot + 0) = UINT64_MAX;
	BARRIER();
	*(volatile uint64_t *)(slot + 8) = now;
	*(volatile uint32_t *)(slot + 16) = (uint32_t)size;
	*(volatile int32_t *)(slot + 20) = frames_lost;
	*(volatile uint32_t *)(slot + 24) = recovered ? 1u : 0u;
	*(volatile uint32_t *)(slot + 28) = flags;
	memcpy(slot + FRAMETAP_SLOT_HEADER, buf, size);
	BARRIER();
	*(volatile uint64_t *)(slot + 0) = seq;
	XCHG64(H64(32), seq + 1);
	signal_event();
}

bool frametap_take_idr_request(void)
{
	if(!g_base)
		return false;
	return CAS32(H32(40), 0, 1) == 1;
}

uint32_t frametap_take_press_request(uint32_t *hold_ms)
{
	if(!g_base)
		return 0;
	uint32_t buttons = XCHG32(H32(68), 0);
	if(buttons && hold_ms)
	{
		uint32_t h = *H32(72);
		*hold_ms = h < 20 ? 20 : h > 2000 ? 2000 : h;
	}
	return buttons;
}

void frametap_press_sent(void)
{
	if(!g_base)
		return;
	uint64_t now = now_ticks();
	XCHG64(H64(80), now);
	INC32(H32(76));
}

uint32_t frametap_audio_delay_ms(void)
{
	if(!g_base || *H32(0) != FRAMETAP_MAGIC)
		return 0;
	uint32_t ms = *H32(88);
	uint64_t freq = *H64(48);
	if(!ms || !freq)
		return 0;
	uint64_t now = now_ticks();
	if((int64_t)(now - *H64(56)) > (int64_t)(2 * freq)) // SmoovPlay stopped
		return 0;
	return ms > 500 ? 500 : ms;
}

void frametap_audio_format(uint32_t rate, uint32_t channels)
{
	if(!rate || !channels || channels > 8)
		return;
	g_arate = rate;
	g_achannels = channels;
	if(g_abase && *A32(0) == FRAMETAP_AUDIO_MAGIC)
	{
		*A32(16) = rate;
		*A32(20) = channels;
	}
}

void frametap_push_audio(const uint8_t *buf, size_t size)
{
	if(!g_abase || *A32(0) != FRAMETAP_AUDIO_MAGIC || !buf || !size)
		return;
	uint64_t now = now_ticks();
	uint64_t freq = *A64(32);
	size_t frame_bytes = 2u * g_achannels;
	size_t cap = (FRAMETAP_AUDIO_SLOT_SIZE - FRAMETAP_AUDIO_SLOT_HEADER) / frame_bytes * frame_bytes;
	size_t done = 0;
	while(done < size)
	{
		size_t n = size - done < cap ? size - done : cap;
		// A chunk too big for one slot goes in several; each slot's time is when its first sample arrived.
		uint64_t q = now;
		if(done && freq)
			q += (uint64_t)((double)(done / frame_bytes) * (double)freq / (double)g_arate);
		uint64_t seq = *A64(24);
		uint8_t *slot = g_abase + FRAMETAP_AUDIO_HEADER + (size_t)(seq % FRAMETAP_AUDIO_SLOT_COUNT) * FRAMETAP_AUDIO_SLOT_SIZE;
		*(volatile uint64_t *)(slot + 0) = UINT64_MAX;
		BARRIER();
		*(volatile uint64_t *)(slot + 8) = q;
		*(volatile uint32_t *)(slot + 16) = (uint32_t)n;
		*(volatile uint32_t *)(slot + 20) = g_arate;
		*(volatile uint32_t *)(slot + 24) = g_achannels;
		memcpy(slot + FRAMETAP_AUDIO_SLOT_HEADER, buf + done, n);
		BARRIER();
		*(volatile uint64_t *)(slot + 0) = seq;
		XCHG64(A64(24), seq + 1);
		done += n;
	}
}

void frametap_end(void)
{
	if(!g_base)
		return;
	// Keep the mapping open (the reader may still hold it); just mark the stream as stopped.
	XCHG32(H32(0), 0);
	if(g_abase)
		XCHG32(A32(0), 0);
	signal_event();
}

#else // neither Windows nor Linux: compiled in, does nothing

bool frametap_enabled(void) { return false; }
bool frametap_begin(uint32_t codec, uint32_t width, uint32_t height, uint32_t max_fps)
{
	(void)codec; (void)width; (void)height; (void)max_fps;
	return false;
}
void frametap_push(const uint8_t *buf, size_t size, int32_t frames_lost, bool recovered)
{
	(void)buf; (void)size; (void)frames_lost; (void)recovered;
}
bool frametap_take_idr_request(void) { return false; }
uint32_t frametap_take_press_request(uint32_t *hold_ms) { (void)hold_ms; return 0; }
uint32_t frametap_audio_delay_ms(void) { return 0; }
void frametap_audio_format(uint32_t rate, uint32_t channels) { (void)rate; (void)channels; }
void frametap_push_audio(const uint8_t *buf, size_t size) { (void)buf; (void)size; }
void frametap_press_sent(void) {}
void frametap_end(void) {}

#endif
