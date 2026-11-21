// Tiny late-bound hooks so property panels can open the symbol library without importing the Draw panel.
let symbolsOpener = () => {};
export const setSymbolsOpener = (fn) => { symbolsOpener = fn; };
export const openSymbols = (id) => symbolsOpener(id);
