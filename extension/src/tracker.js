// Counts real watch time for one page: only while the video is actually playing (not paused, buffering, seeking
// or showing an ad). Pure logic with an injectable clock so it can be tested without YouTube.
(function (root) {
  const MAX_TICK = 2000; // ms; a longer gap (tab frozen, laptop asleep) counts at most this much

  class Tracker {
    constructor(now) {
      this.now = now || (() => performance.now());
      this.session = null;
      this.last = null;
    }

    newSessionId() {
      return "ext-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
    }

    // state: {videoId, playing, adShowing, position, duration, title}
    tick(state) {
      const t = this.now();
      const flushed = [];
      if (!state.videoId) {
        if (this.session) flushed.push(this.end());
        this.last = t;
        return flushed;
      }
      if (!this.session || this.session.video_id !== state.videoId) {
        if (this.session) flushed.push(this.end());
        this.session = { session_id: this.newSessionId(), video_id: state.videoId, seconds: 0, position: 0,
                         title: state.title || "", length: state.duration || null, dirty: false };
        this.last = t;
      }
      const dt = Math.min(Math.max(t - (this.last ?? t), 0), MAX_TICK);
      this.last = t;
      if (state.playing && !state.adShowing) {
        this.session.seconds += dt / 1000;
        this.session.dirty = true;
      }
      if (!state.adShowing) {
        this.session.position = state.position || this.session.position;
        if (state.duration && isFinite(state.duration)) this.session.length = state.duration;
      }
      if (state.title) this.session.title = state.title;
      return flushed;
    }

    snapshot() {
      if (!this.session) return null;
      const s = this.session;
      return { session_id: s.session_id, video_id: s.video_id, seconds: Math.round(s.seconds * 10) / 10,
               position: Math.round(s.position), title: s.title, length: s.length ? Math.round(s.length) : null };
    }

    end() {
      const snap = this.snapshot();
      this.session = null;
      return snap;
    }
  }

  root.KBTracker = Tracker;
})(typeof self !== "undefined" ? self : this);
