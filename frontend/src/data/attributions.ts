/**
 * Licence credits for catalog titles whose media is third-party open content.
 * Shown on the title page (CC BY requires attribution where the work is shown).
 */
export interface Attribution {
  text: string;
  license: string;
  licenseUrl: string;
}

export const ATTRIBUTIONS: Record<string, Attribution> = {
  tt2285752: {
    text: '(c) Blender Foundation | mango.blender.org',
    license: 'CC BY 3.0',
    licenseUrl: 'https://creativecommons.org/licenses/by/3.0/',
  },
};
