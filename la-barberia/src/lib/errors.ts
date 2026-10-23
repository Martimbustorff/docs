export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

export const Errors = {
  validation: (msg = "Dados inválidos. Verifique os campos e tente novamente.") => new ApiError(400, "VALIDATION", msg),
  unauthorized: () => new ApiError(401, "UNAUTHORIZED", "Sessão inválida ou expirada."),
  forbidden: (msg = "Operação não permitida.") => new ApiError(403, "FORBIDDEN", msg),
  notFound: (msg = "Não encontrado.") => new ApiError(404, "NOT_FOUND", msg),
  slotTaken: () => new ApiError(409, "SLOT_TAKEN", "Esse horário já não está disponível. Escolha outro, por favor."),
  conflict: (msg: string) => new ApiError(409, "CONFLICT", msg),
  rateLimited: () => new ApiError(429, "RATE_LIMITED", "Demasiados pedidos. Aguarde um pouco e tente novamente."),
  closed: () => new ApiError(403, "BOOKING_CLOSED", "As marcações online estão temporariamente fechadas. Contacte-nos por WhatsApp."),
};
