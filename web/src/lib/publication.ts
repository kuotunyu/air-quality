import raw from "../data/pages-publication.json";

export interface PagesPublication {
  schema_version: 1;
  metadata: string[];
  l0: string[];
  l1: string[];
  l2: string[];
}

export const pagesPublication = raw as PagesPublication;
export const publishedFileSet = new Set([
  ...pagesPublication.metadata,
  ...pagesPublication.l0,
  ...pagesPublication.l1,
  ...pagesPublication.l2,
]);
