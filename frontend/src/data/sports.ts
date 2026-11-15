import type { LiveGame } from './types';

/**
 * Live sports: one synthetic football match (SYN-MAT-0001). Teams, players,
 * competition and venue are fictional; the broadcast is the 60 s football clip
 * and its live data comes from the match-state API
 * (backend/live-sports-agent/build_synthetic_match.py). The values below are the
 * offline fallback shown when no API is configured.
 */

export const SOCCER_GAME: LiveGame = {
  id: 'g-soccer',
  sport: 'soccer',
  league: 'Northshore Championship',
  venue: 'Harborside Arena',
  // The state at the "Join Live" point (the start of the broadcast clip).
  home: { code: 'HCF', name: 'Harbor City FC', city: 'Harbor City', hue: 205, score: 1, record: '14W-7D-6L' },
  away: { code: 'RCA', name: 'Redcliffe Athletic', city: 'Redcliffe', hue: 355, score: 1, record: '15W-5D-7L' },
  clock: "78'",
  status: '2nd Half',
  viewers: '84K watching',
  keyPlays: [
    { clock: "67'", label: 'Shot saved', detail: "Whitlock's effort kept out by Brandvold", team: 'away', kind: 'chance' },
    { clock: "61'", label: 'GOAL — Harbor City', detail: 'Okafor-Hale heads in a corner, 1–1', team: 'home', kind: 'goal' },
    { clock: "56'", label: 'Yellow card', detail: 'Nordvik-Ames (Redcliffe)', team: 'away', kind: 'foul' },
    { clock: "23'", label: 'GOAL — Redcliffe', detail: 'Ibe-Castro opens the scoring, 0–1', team: 'away', kind: 'goal' },
    { clock: "1'", label: 'Kickoff', detail: 'Underway at the Harborside Arena', team: 'away', kind: 'start' },
  ],
  players: [
    { name: 'D. Okafor-Hale', team: 'home', line: '1 Goal · 1 Shot', star: true, hot: true, form: 84 },
    { name: 'T. Lark', team: 'home', line: '1 Shot on target', form: 74 },
    { name: 'K. Asante-Brook', team: 'home', line: '1 Shot', form: 70 },
    { name: 'M. Ibe-Castro', team: 'away', line: '1 Goal · 1 Offside', star: true, form: 81 },
  ],
  angles: [
    { id: 'a-main', label: 'Main Broadcast', detail: 'Tactical wide', hue: 205, live: true },
    { id: 'a-tac', label: 'Tactical Cam', detail: 'High behind goal', hue: 150 },
    { id: 'a-net', label: 'Net Cam', detail: 'Inside the goal', hue: 355 },
    { id: 'a-crowd', label: 'Pitchside', detail: 'Low field level', hue: 275 },
  ],
};

export const LIVE_GAMES: LiveGame[] = [SOCCER_GAME];

/**
 * Sports entities that appear in this app's live content, keyed by the
 * canonical ids the Settings API resolver stores in a profile's `followed`
 * list (infra/lambdas/profile_settings/sports_entities.json). A real feed would
 * tag its events with these ids; this demo's content is static, so each entry
 * lists the spellings its sources use (`names`: how the match feed, rosters and
 * highlights write it). A followed id with no entry here has no effect on
 * sports screens.
 */
export type SportsEntityKind = 'team' | 'player';

export interface SportsEntity {
  id: string;
  kind: SportsEntityKind;
  name: string;
  gameId: string;
  names: string[];
}

const soccerPlayer = (slug: string, name: string, roster?: string): SportsEntity => ({
  id: `player:${slug}`, kind: 'player', name, gameId: SOCCER_GAME.id, names: roster ? [name, roster] : [name],
});

export const SPORTS_ENTITIES: SportsEntity[] = [
  { id: 'team:harbor-city-fc', kind: 'team', name: 'Harbor City FC', gameId: SOCCER_GAME.id,
    names: ['Harbor City FC', 'Harbor City'] },
  { id: 'team:redcliffe-athletic', kind: 'team', name: 'Redcliffe Athletic', gameId: SOCCER_GAME.id,
    names: ['Redcliffe Athletic', 'Redcliffe'] },
  // Harbor City FC
  soccerPlayer('ilya-brandvold', 'Ilya Brandvold', 'I. Brandvold'),
  soccerPlayer('mateo-quintal', 'Mateo Quintal', 'M. Quintal'),
  soccerPlayer('haruto-senda', 'Haruto Senda', 'H. Senda'),
  soccerPlayer('desmond-okafor-hale', 'Desmond Okafor-Hale', 'D. Okafor-Hale'),
  soccerPlayer('jonah-vesterberg', 'Jonah Vesterberg', 'J. Vesterberg'),
  soccerPlayer('callum-ashdown', 'Callum Ashdown', 'C. Ashdown'),
  soccerPlayer('tobiah-lark', 'Tobiah Lark', 'T. Lark'),
  soccerPlayer('bram-eijsden', 'Bram Eijsden', 'B. Eijsden'),
  soccerPlayer('soren-dahlquist', 'Soren Dahlquist', 'S. Dahlquist'),
  soccerPlayer('luca-venturelli', 'Luca Venturelli', 'L. Venturelli'),
  soccerPlayer('kofi-asante-brook', 'Kofi Asante-Brook', 'K. Asante-Brook'),
  soccerPlayer('amir-rostami-vale', 'Amir Rostami-Vale', 'A. Rostami-Vale'),
  soccerPlayer('felipe-arandela', 'Felipe Arandela', 'F. Arandela'),
  soccerPlayer('niall-corrigan', 'Niall Corrigan', 'N. Corrigan'),
  // Redcliffe Athletic
  soccerPlayer('marek-zielonka', 'Marek Zielonka', 'M. Zielonka'),
  soccerPlayer('hugo-delacroix-ward', 'Hugo Delacroix-Ward', 'H. Delacroix-Ward'),
  soccerPlayer('elias-nordvik-ames', 'Elias Nordvik-Ames', 'E. Nordvik-Ames'),
  soccerPlayer('oskar-thalberg', 'Oskar Thalberg', 'O. Thalberg'),
  soccerPlayer('samuel-adeyinka-rhodes', 'Samuel Adeyinka-Rhodes', 'S. Adeyinka-Rhodes'),
  soccerPlayer('rafael-monteverde', 'Rafael Monteverde', 'R. Monteverde'),
  soccerPlayer('declan-moorcroft', 'Declan Moorcroft', 'D. Moorcroft'),
  soccerPlayer('marlon-ibe-castro', 'Marlon Ibe-Castro', 'M. Ibe-Castro'),
  soccerPlayer('teodor-vasko', 'Teodor Vasko', 'T. Vasko'),
  soccerPlayer('jarrah-whitlock', 'Jarrah Whitlock', 'J. Whitlock'),
  soccerPlayer('finn-ostergaard', 'Finn Ostergaard', 'F. Ostergaard'),
  soccerPlayer('ruben-szabo-kent', 'Ruben Szabo-Kent', 'R. Szabo-Kent'),
  soccerPlayer('idris-rahimzadeh', 'Idris Rahimzadeh', 'I. Rahimzadeh'),
  soccerPlayer('cormac-lyle', 'Cormac Lyle', 'C. Lyle'),
];

export const sportsEntityById = new Map(SPORTS_ENTITIES.map((e) => [e.id, e]));

export const gameById = (id: string): LiveGame =>
  LIVE_GAMES.find((g) => g.id === id) ?? LIVE_GAMES[0];
