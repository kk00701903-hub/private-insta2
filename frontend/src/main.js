import { registerSW } from "virtual:pwa-register";
import "./style.css";
import "./app-extra.css";
import "./monetize.css";
import { boot } from "./app.js";
import { initMonetize } from "./monetize.js";

registerSW({ immediate: true });
boot();
initMonetize();
