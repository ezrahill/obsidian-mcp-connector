import { describe, expect, test } from "bun:test";
import { type } from "arktype";
import { searchRequest } from "./smart-search";

describe("searchRequest — POST /search/smart body validator", () => {
  // Regression for the "must be a string (was an object)" 400 returned
  // by every search_vault_smart MCP call. The previous schema was
  //   type("string.json.parse").to(searchRequest)
  // which assumed `req.body` was a raw JSON string. The route is mounted
  // on Express with bodyParser.json() upstream, so the body is already a
  // parsed object — running it through string.json.parse rejected every
  // real request. The validator must accept the parsed-object shape.

  test("accepts a minimal parsed-object body with just a query", () => {
    const result = searchRequest({ query: "hello" });
    expect(result).not.toBeInstanceOf(type.errors);
    if (!(result instanceof type.errors)) {
      expect(result.query).toBe("hello");
    }
  });

  test("accepts a body with an optional filter object", () => {
    const body = {
      query: "semantic",
      filter: {
        folders: ["Public"],
        excludeFolders: ["Archive"],
        limit: 10,
      },
    };
    const result = searchRequest(body);
    expect(result).not.toBeInstanceOf(type.errors);
  });

  test("accepts an empty filter object", () => {
    const result = searchRequest({ query: "x", filter: {} });
    expect(result).not.toBeInstanceOf(type.errors);
  });

  test("rejects a missing query", () => {
    const result = searchRequest({ filter: { limit: 5 } });
    expect(result).toBeInstanceOf(type.errors);
  });

  test("rejects an empty query string", () => {
    const result = searchRequest({ query: "" });
    expect(result).toBeInstanceOf(type.errors);
  });

  test("rejects a JSON string body (the previous schema accepted this — the new one must not)", () => {
    // Sanity check that we are no longer trying to JSON.parse the body
    // ourselves: a stringified body should fail validation, because
    // bodyParser.json() will have already produced an object before we
    // get here. If this passes, the double-parse bug has crept back in.
    const result = searchRequest('{"query":"hello"}' as unknown as object);
    expect(result).toBeInstanceOf(type.errors);
  });
});
