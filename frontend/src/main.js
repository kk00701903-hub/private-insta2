import { registerSW } from "virtual:pwa-register";
import "./style.css";
import "./app-extra.css";
import { boot } from "./app.js";

registerSW({ immediate: true });
boot();
