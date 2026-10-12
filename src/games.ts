// Catalog metadata is distinct from the game's runtime capabilities.
export type Game = {
  id: string;
  title: string;
  artwork: string;
  newsLanguage: string;
};
export const games: Game[] = [
  {
    id: "chaos-zero-nightmare",
    title: "Chaos Zero Nightmare",
    artwork: "/chaos-keyart.webp",
    newsLanguage: "en",
  },
];
