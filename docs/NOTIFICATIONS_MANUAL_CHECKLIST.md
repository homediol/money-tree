# Sound and Voice Notification Manual Check

Run this checklist in the target browser from **Settings → Notification Test Center**. Browser playback checks do not prove physical speaker audibility; record that only from the user confirmation control.

1. Press **Enable Sounds** and confirm the audio status says `ready`. A blocked or unsupported state should show an actionable message.
2. Preview each of the ten built-in tones. Confirm Play, Stop and Replay work. After playback ends, record **Yes, I heard it** or **No, I did not hear it**. The result is stored in `winner_predict_audio_audibility_tests` in this browser's local storage.
3. Select **Use This Sound** for an event. Saving live sound settings must remain blocked until you have heard and confirmed every selected sound. Save settings, reload the page, and confirm the event selection and volume persisted.
4. Preview each listed browser voice, change language/rate/pitch/volume, stop and replay. Confirm unsupported SpeechSynthesis or Kinyarwanda availability is stated accurately. Enabling live voice must remain blocked until you confirm hearing that selected voice and language.
5. Run **TEST NOTIFICATION** with sound and voice separately and together. Confirm the visual toast is marked `TEST`, and confirm no SHADOW result, Rules Performance, Behaviour Detection, ADIE, or historical analytics change.
6. Enable the master switch and one live event, then observe a real source event over the existing `/ws/live` connection. Confirm duplicates/cooldown suppress repeats and live source event IDs/round IDs are retained.
7. Mute all, disable a module event, set quiet hours, and change priority. Confirm visual alerts remain while muted and configured audio/voice is suppressed as expected.
8. Suspend and resume the browser tab, then disconnect/reconnect the live transport. Confirm the UI reports blocked playback or reconnection accurately and does not produce overlapping speech.

The dashboard must be open for these page-level sounds and voice announcements. Browser suspension, OS audio settings, device routing, browser policy, and unavailable voices can prevent audible output.
