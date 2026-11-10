// Builds app/static/panel.css from the classes used in the templates:
//   npx tailwindcss@3.4.16 -c tailwind.config.js -i app/static/src/panel.input.css -o app/static/panel.css --minify
// CI fails if the committed panel.css is out of date.
module.exports = {
  content: ["./app/templates/**/*.html"],
  theme: { extend: {} },
  plugins: [],
};
