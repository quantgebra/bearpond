// the pages work without this; it only makes them nicer: times in the viewer's own time zone (the server writes them in
// UTC), the switch between the light and the dark theme, the menu that can be hidden, and a search that runs as it is typed

const timeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

for (const element of document.querySelectorAll("time[datetime]")) {
    const when = new Date(element.getAttribute("datetime"));
    if (!Number.isNaN(when.getTime())) {
        element.textContent = timeFormat.format(when);
        element.title = when.toISOString();
    }
}

for (const button of document.querySelectorAll("[data-theme-toggle]")) {
    button.addEventListener("click", () => {
        const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
        document.documentElement.dataset.theme = theme;
        localStorage.setItem("bearpond-theme", theme);
    });
}

for (const button of document.querySelectorAll("[data-sidebar-toggle]")) {
    button.addEventListener("click", () => {
        const isNarrow = window.matchMedia("(max-width: 860px)").matches;
        document.body.classList.toggle(isNarrow ? "sidebar-shown" : "sidebar-hidden");
    });
}

for (const input of document.querySelectorAll("[data-submit-on-input]")) {
    let timer = undefined;
    input.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(() => input.form.requestSubmit(), 350);
    });
    // the search survives the page being made again, with the cursor where it was
    if (input.value !== "") {
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
    }
}

// the link that is shown once is copied with a button, or selected whole when the field is clicked
for (const field of document.querySelectorAll("[data-select-on-focus]")) {
    field.addEventListener("focus", () => field.select());
}

for (const button of document.querySelectorAll("[data-copy]")) {
    button.addEventListener("click", async () => {
        const field = button.parentElement.querySelector("input");
        await navigator.clipboard.writeText(field.value);
        button.textContent = "Copied";
        setTimeout(() => { button.textContent = "Copy"; }, 1500);
    });
}

// one picker of groups is open at a time, and a click elsewhere closes it
document.addEventListener("click", (event) => {
    for (const picker of document.querySelectorAll("details.picker[open]")) {
        if (!picker.contains(event.target)) {
            picker.open = false;
        }
    }
});
