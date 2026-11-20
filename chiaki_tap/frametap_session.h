// SmoovPlay's frame tap, as chiaki-ng's StreamSession uses it. Everything the tap does inside chiaki-ng lives in
// frametap_session.cpp; chiaki-ng's own files only get one-line calls to these (apply_patch.py) and one friend line
// in streamsession.h, so a new chiaki-ng version is quick to patch.
//
// With CHIAKI_FRAMETAP unset, Begin() returns false and chiaki-ng runs exactly as without the tap.
#pragma once

#include <chiaki/session.h>

#include <QByteArray>

#include <stddef.h>
#include <stdint.h>

class StreamSession;

class FrameTapSession
{
	public:
		// At stream start, in place of setting chiaki-ng's video callback. True if the tap is on and has set its own
		// callback (which copies each frame out, then hands it to chiaki-ng's decoder); false: set chiaki-ng's own.
		static bool Begin(StreamSession *session, ChiakiSession *chiaki_session, const ChiakiConnectVideoProfile &profile);
		// After chiaki_session_fini.
		static void End();
		// When chiaki-ng sets up its sound output.
		static void AudioFormat(unsigned int rate, size_t sample_size);
		// In place of QueueAudioOutData: holds the sound back by SmoovPlay's picture delay when SmoovPlay asks.
		static void QueueAudio(StreamSession *session, const QByteArray &buf);
		// At the top of PushAudioFrame: the game's sound as it arrives, for SmoovPlay's instant replay.
		static void PushAudio(StreamSession *session, const int16_t *buf, size_t samples_count);

	private:
		static bool VideoSample(uint8_t *buf, size_t buf_size, int32_t frames_lost, bool frame_recovered, void *user);
		static void Press(StreamSession *session, uint32_t buttons, uint32_t hold_ms);
		static void Quit(StreamSession *session, uint32_t how);
};
