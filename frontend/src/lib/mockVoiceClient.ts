/**
 * Mock implementation of VoiceClient — simulates the full voice loop with
 * timers so the Search UX works today, before the Nova Sonic backend exists.
 * Results are drawn from the local mock catalog.
 *
 * Swap this for a `NovaSonicVoiceClient` (same interface) once the backend
 * path (Fargate vs. AgentCore-bidi) is chosen.
 */

import { VOD_TITLES } from '../data/titles';
import type { VoiceClient, VoiceEvents, VoiceResult } from './voiceClient';

/** A canned demo query + its "assistant" reply + which titles it surfaces. */
interface Scenario {
  query: string;
  reply: string;
  resultIds: string[];
  whys: Record<string, string>;
  followups: string[];
}

const SCENARIOS: Scenario[] = [
  {
    query: 'a feel-good movie under two hours',
    reply:
      "Got it — here are a few light, feel-good picks that won't run long. Want me to narrow it down?",
    resultIds: ['tt1001164', 'tt1001009', 'tt1001444', 'md0000002'],
    whys: {
      tt1001164: 'Witty, feel-good comedy with real heart',
      tt1001009: 'Bright and fun — a real crowd-pleaser',
      tt1001444: "Warm and uplifting, an easy watch you'll finish",
      md0000002: 'A tiny love story, over in thirty seconds',
    },
    followups: ['Even shorter', 'More upbeat', 'Something romantic', 'Something animated'],
  },
  {
    query: 'something like Tears of Steel',
    reply:
      'Here are some sci-fi picks in the same vein as Tears of Steel.',
    resultIds: ['tt1001163', 'tt1001183', 'tt1001329', 'md0000004'],
    whys: {
      tt1001163: 'Big-idea sci-fi with real stakes',
      tt1001183: 'Colorful sci-fi for the whole family',
      tt1001329: 'Far-future adventure with a sense of wonder',
      md0000004: 'Neon-soaked sci-fi action in thirty seconds',
    },
    followups: ['More action', 'Less intense', 'Something funnier', 'Something newer'],
  },
];

export class MockVoiceClient implements VoiceClient {
  private ev: VoiceEvents;
  private timers: ReturnType<typeof setTimeout>[] = [];
  private scenarioIx = 0;

  constructor(events: VoiceEvents) {
    this.ev = events;
  }

  private at(ms: number, fn: () => void) {
    this.timers.push(setTimeout(fn, ms));
  }

  private clear() {
    this.timers.forEach(clearTimeout);
    this.timers = [];
  }

  start() {
    this.clear();
    const scenario = SCENARIOS[this.scenarioIx % SCENARIOS.length];
    this.scenarioIx += 1;

    this.ev.onState?.('listening');

    // Stream the transcript word-by-word to feel like live STT.
    const words = scenario.query.split(' ');
    let acc = '';
    words.forEach((w, i) => {
      this.at(220 + i * 180, () => {
        acc = acc ? `${acc} ${w}` : w;
        this.ev.onPartialTranscript?.(acc);
      });
    });

    // Hold-to-talk: transcript streams while held; `stop()` (release) submits.
  }

  stop() {
    // In the mock, stopping early just jumps to finalizing the current scenario.
    const scenario = SCENARIOS[(this.scenarioIx - 1 + SCENARIOS.length) % SCENARIOS.length];
    this.clear();
    this.finalize(scenario);
  }

  submitText(text: string) {
    this.clear();
    this.ev.onFinalTranscript?.(text);
    // Reuse the first scenario's results for any typed query in the mock.
    this.runThinking(SCENARIOS[0]);
  }

  cancel() {
    this.clear();
    this.ev.onState?.('idle');
  }

  private finalize(scenario: Scenario) {
    this.ev.onFinalTranscript?.(scenario.query);
    this.runThinking(scenario);
  }

  private runThinking(scenario: Scenario) {
    this.ev.onState?.('thinking');
    this.at(120, () => this.ev.onIntent?.('recommendation'));
    // Simulate the agent's streamed status events (the filler-speech source).
    this.at(150, () => this.ev.onStatus?.('Understanding your request…'));
    this.at(900, () => this.ev.onStatus?.('Searching the catalog…'));
    this.at(1700, () => this.ev.onStatus?.('Picking the best matches…'));

    this.at(2400, () => {
      const results = this.buildResults(scenario);
      this.ev.onResponse?.(scenario.reply);
      this.ev.onResults?.(results);
      this.ev.onFollowups?.(scenario.followups);
      this.ev.onState?.('results');
    });
  }

  private buildResults(scenario: Scenario): VoiceResult[] {
    return scenario.resultIds
      .map((id) => {
        const t = VOD_TITLES.find((x) => x.id === id);
        if (!t) return null;
        return {
          id: t.id,
          title: t.name,
          why: scenario.whys[id] ?? t.whyYoullLove,
          hueA: t.hueA,
          hueB: t.hueB,
          genres: t.genres,
          year: t.year,
          rating: t.imdb,
          maturity: t.maturity,
          plot: t.synopsis,
          cast: t.cast,
          kind: t.kind,
        } as VoiceResult;
      })
      .filter((r): r is VoiceResult => r !== null);
  }
}
