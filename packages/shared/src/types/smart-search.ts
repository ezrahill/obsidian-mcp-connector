import { type } from "arktype";
import * as SmartConnections from "./plugin-smart-connections";

export const searchRequest = type({
  query: type("string>0").describe("A search phrase for semantic search"),
  "filter?": {
    "folders?": type("string[]").describe(
      'An array of folder names to include. For example, ["Public", "Work"]',
    ),
    "excludeFolders?": type("string[]").describe(
      'An array of folder names to exclude. For example, ["Private", "Archive"]',
    ),
    "limit?": type("number>0").describe(
      "The maximum number of results to return",
    ),
  },
});

export interface SearchResponse {
  results: Array<{
    path: string;
    text: string;
    score: number;
    breadcrumbs: string;
  }>;
}

export const searchParameters = type({
  query: "string",
  filter: SmartConnections.SmartSearchFilter,
});
