document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-password-visibility-toggle]").forEach((button) => {
    const fieldId = button.getAttribute("aria-controls");
    const field = fieldId ? document.getElementById(fieldId) : null;
    const showIcon = button.querySelector("[data-eye-show]");
    const hideIcon = button.querySelector("[data-eye-hide]");

    if (!field || !showIcon || !hideIcon) return;

    button.addEventListener("click", () => {
      const reveal = field.type === "password";
      field.type = reveal ? "text" : "password";
      button.setAttribute("aria-pressed", String(reveal));
      button.setAttribute("aria-label", reveal ? "Hide password" : "Show password");
      showIcon.hidden = !reveal;
      hideIcon.hidden = reveal;
    });
  });
});
