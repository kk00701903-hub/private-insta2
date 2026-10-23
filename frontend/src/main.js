import { registerSW } from "virtual:pwa-register";
import "./style.css";
import "./app-extra.css";
import "./ai-reels.css";
import { boot } from "./app.js";
import { mountAiReels } from "./ai-reels.js";

registerSW({ immediate: true });
mountAiReels(); // injects the AI 릴스 view/nav before boot() binds navigation
boot();
