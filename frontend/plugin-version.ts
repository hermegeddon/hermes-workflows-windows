import { readFileSync } from "node:fs";
export const PLUGIN_VERSION = JSON.parse(readFileSync(new URL("../hermes-workflows-py/pyproject.toml", import.meta.url), "utf8").match(/version = ("[^"\n]+")/)![1]!) as string;
