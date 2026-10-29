// Currawong knob: the "feel" maths that doesn't touch hardware, kept here so it can be tested on a
// PC (test/test_feel.cpp: g++ -std=c++17 -Isrc test/test_feel.cpp && ./a.out).
#pragma once
#include <math.h>
#include <stdint.h>

namespace feel {

constexpr float kPi = 3.14159265f;

// Flick to coast: 1 while turning slowly, easing to 0 between `from` and `to` (rad/s), so a fast
// spin glides through a long list and the clicks come back as it slows. Smoothstep, no corners.
inline float coastFactor(float speed, float from, float to) {
  if (speed <= from) return 1.0f;
  if (speed >= to) return 0.0f;
  float t = (speed - from) / (to - from);
  return 1.0f - t * t * (3.0f - 2.0f * t);
}

// One detent hill: no force inside the dead zone, then half a sine that peaks midway and returns
// to zero at `edge`. It starts from zero rather than with a step, so sensor noise at the dead-zone
// edge can't flick the force on and off (that was the buzz at rest).
inline float hill(float err, float deadzone, float edge, float peak) {
  float a = fabsf(err);
  if (a <= deadzone || a >= edge) return 0;
  float f = peak * sinf(kPi * (a - deadzone) / (edge - deadzone));
  return err > 0 ? -f : f;
}

// The magnet's pull (volts) at `x` radians from its detent: a smooth well `reach` wide each side,
// zero at the centre (so it holds nothing at rest) and zero at the edge (no step to feel).
inline float magnetPull(float x, float reach, float volts) {
  if (reach <= 0 || fabsf(x) >= reach) return 0.0f;
  return -volts * sinf(kPi * x / reach);
}

// Landmark positions: a sorted set of up to 64, checked by binary search every loop.
struct Marks {
  static constexpr int kMax = 64;
  uint16_t v[kMax];
  int n = 0;

  void clear() { n = 0; }

  // Keeps them sorted and unique; ignores negatives and anything past kMax.
  void set(const int* p, int count) {
    n = 0;
    for (int i = 0; i < count && n < kMax; i++) {
      if (p[i] < 0 || p[i] > 65535) continue;
      uint16_t x = (uint16_t)p[i];
      int j = n;
      while (j > 0 && v[j - 1] > x) j--;
      if (j > 0 && v[j - 1] == x) continue;
      for (int k = n; k > j; k--) v[k] = v[k - 1];
      v[j] = x;
      n++;
    }
  }

  bool has(int p) const {
    if (p < 0) return false;
    int lo = 0, hi = n - 1;
    while (lo <= hi) {
      int mid = (lo + hi) / 2;
      if (v[mid] == p) return true;
      if (v[mid] < p) lo = mid + 1;
      else hi = mid - 1;
    }
    return false;
  }
};

// "3,17,-1,42" -> {3, 17, -1, 42}. Returns how many were read (at most `max`). Stops at anything
// that isn't a number or a comma, so a truncated line can't read past its end.
inline int parseInts(const char* s, int* out, int max) {
  int count = 0;
  while (*s && count < max) {
    while (*s == ',' || *s == ' ') s++;
    if (!*s) break;
    bool neg = false;
    if (*s == '-') { neg = true; s++; }
    if (*s < '0' || *s > '9') break;
    long x = 0;
    while (*s >= '0' && *s <= '9') { x = x * 10 + (*s - '0'); if (x > 1000000) x = 1000000; s++; }
    out[count++] = (int)(neg ? -x : x);
  }
  return count;
}

}  // namespace feel
